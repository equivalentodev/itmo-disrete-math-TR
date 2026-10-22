from __future__ import annotations

from collections import defaultdict
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


def render_documents(
    templates: dict[str, str], settings: Settings, bank: Bank,
    students: list[Student] | None = None, records: dict | None = None,
) -> dict[str, str]:
    head = templates['head.tex']
    for key, value in {'COURSE': settings.course, 'TERM': settings.term, 'NUMBER': bank.name[2:]}.items():
        head = head.replace('@@' + key + '@@', escape(value))
    tail = templates['tail.tex']
    if students is None:
        parts = [head]
        for n, task in enumerate(bank.tasks, 1):
            for k, text in enumerate(task, 1):
                parts.append(
                    f'\n\\medskip\n\\noindent\\textbf{{Вариант {k}}}\\par\n'
                    '\\begin{questions}\n'
                    f'\\setcounter{{question}}{{{n - 1}}}\n'
                    + text + '\\end{questions}\n'
                )
        return {'dump.tex': ''.join(parts) + tail}
    by_flow = defaultdict(list)
    for student in students:
        by_flow[student.flow].append(student)
    result = {}
    for flow in sorted(by_flow, key=lambda f: (not f.isdigit(), int(f) if f.isdigit() else f)):
        parts = [head]
        for s in sorted(by_flow[flow], key=lambda s: (s.group, s.name, s.isu_id)):
            parts.extend([
                templates['qStart.tex'],
                f'\\textbf{{ФИО:}} & {escape(s.name)}',
                templates['qStart2.tex'],
            ])
            rec = records[s.isu_id]['variants']
            for n, variants in enumerate(bank.tasks, 1):
                parts.append(variants[rec[str(n)] - 1])
            parts.append(templates['qFinish.tex'])
        result[f'flow_{flow}.tex'] = ''.join(parts) + tail
    return result
