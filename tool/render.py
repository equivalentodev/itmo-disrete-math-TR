from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from .inputs import Bank, Settings, Student, ToolError


def escape(text: str) -> str:
    replacements = {
        '\\': r'\textbackslash{}', '&': r'\&', '%': r'\%', '$': r'\$', '#': r'\#',
        '_': r'\_', '{': r'\{', '}': r'\}', '~': r'\textasciitilde{}', '^': r'\textasciicircum{}',
    }
    return ''.join(replacements.get(c, c) for c in text)


def read_templates(root: Path) -> dict[str, str]:
    names = ('head.tex', 'qStart.tex', 'qStart2.tex', 'qFinish.tex', 'tail.tex')
    try:
        result = {name: (root / 'templates' / name).read_text(encoding='utf-8') for name in names}
    except OSError as exc:
        raise ToolError(f'Не удалось прочитать общие шаблоны: {exc}') from exc
    for token in ('@@COURSE@@', '@@TERM@@', '@@NUMBER@@'):
        if token not in result['head.tex']:
            raise ToolError(f'В шаблоне head.tex отсутствует поле {token}.')
    return result


def document_head(templates: dict[str, str], settings: Settings, bank_name: str) -> str:
    head = templates['head.tex']
    for key, value in {'COURSE': settings.course, 'TERM': settings.term, 'NUMBER': bank_name[2:]}.items():
        head = head.replace('@@' + key + '@@', escape(value))
    return head


def _review_prefix(number: int, variant: int, show_variant: bool) -> str:
    heading = f'\n\\medskip\n\\noindent\\textbf{{Вариант {variant}}}\\par\n' if show_variant else '\n'
    return (
        heading + '\\begin{questions}\n'
        f'\\setcounter{{question}}{{{number - 1}}}\n'
    )


def review_fragment(number: int, variant: int, text: str, *, show_variant: bool = True) -> str:
    return _review_prefix(number, variant, show_variant) + text + '\\end{questions}\n'


@dataclass(frozen=True)
class SourceSpan:
    path: str
    start: int
    end: int
    text: str


class _Document:
    def __init__(self):
        self.parts = []
        self.spans = []
        self.line = 1

    def add(self, text: str, path: str | None = None):
        if path and text:
            self.spans.append(SourceSpan(path, self.line,
                                         self.line + text.count('\n') - int(text.endswith('\n')), text))
        self.parts.append(text)
        self.line += text.count('\n')

    def finish(self, name: str, source_maps: dict | None) -> str:
        if source_maps is not None:
            source_maps[name] = self.spans
        return ''.join(self.parts)


def render_documents(
    templates: dict[str, str], settings: Settings, bank: Bank,
    students: list[Student] | None = None, records: dict | None = None,
    *, source_maps: dict[str, list[SourceSpan]] | None = None,
) -> dict[str, str]:
    head = document_head(templates, settings, bank.name)
    tail = templates['tail.tex']
    if students is None:
        document = _Document()
        document.add(head, 'templates/head.tex')
        for n, task in enumerate(bank.tasks, 1):
            for k, text in enumerate(task, 1):
                document.add(_review_prefix(n, k, True))
                document.add(text, f'TR/{bank.name}/{n}/{k}.tex')
                document.add('\\end{questions}\n')
        document.add(tail, 'templates/tail.tex')
        return {'dump.tex': document.finish('dump.tex', source_maps)}
    by_flow = defaultdict(list)
    for student in students:
        by_flow[student.flow].append(student)
    result = {}
    for flow in sorted(by_flow, key=lambda f: (not f.isdigit(), int(f) if f.isdigit() else f)):
        document = _Document()
        document.add(head, 'templates/head.tex')
        for s in sorted(by_flow[flow], key=lambda s: (s.group, s.name, s.isu_id)):
            document.add(templates['qStart.tex'], 'templates/qStart.tex')
            document.add(f'\\textbf{{ФИО:}} & {escape(s.name)}')
            document.add(templates['qStart2.tex'], 'templates/qStart2.tex')
            rec = records[s.isu_id]['variants']
            for n, variants in enumerate(bank.tasks, 1):
                k = rec[str(n)]
                document.add(variants[k - 1], f'TR/{bank.name}/{n}/{k}.tex')
            document.add(templates['qFinish.tex'], 'templates/qFinish.tex')
        document.add(tail, 'templates/tail.tex')
        name = f'flow_{flow}.tex'
        result[name] = document.finish(name, source_maps)
    return result
