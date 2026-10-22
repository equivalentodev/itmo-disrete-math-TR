from __future__ import annotations

from collections import Counter, defaultdict
from itertools import combinations, product
import math
import random

from .inputs import Student, ToolError


ALGORITHM = 'balanced-flows-v1'
RANDOM_ATTEMPTS = 256
EXACT_SPACE_LIMIT = 256
EXACT_NODE_LIMIT = 250_000


def balanced(rows: list[tuple[int, ...]], counts: tuple[int, ...]) -> bool:
    for column, count in enumerate(counts):
        uses = Counter(row[column] for row in rows)
        frequencies = [uses[v] for v in range(1, count + 1)]
        if max(frequencies) - min(frequencies) > 1:
            return False
    return True


def _exact(counts: tuple[int, ...], sizes: dict[str, int], used: set[tuple[int, ...]], rng: random.Random):
    """Поиск непересекающихся сбалансированных подмножеств, без перестановок строк."""
    candidates = [c for c in product(*(range(1, k + 1) for k in counts)) if c not in used]
    rng.shuffle(candidates)
    flows = sorted(sizes, key=lambda f: -sizes[f])
    solution = {}
    nodes = 0

    def flow_search(flow_index: int, available: list[tuple[int, ...]]) -> bool:
        nonlocal nodes
        if flow_index == len(flows):
            return True
        flow = flows[flow_index]
        n = sizes[flow]
        low = [n // k for k in counts]
        high = [(n + k - 1) // k for k in counts]
        frequencies = [Counter() for _ in counts]
        chosen = []

        def choose(start: int) -> bool:
            nonlocal nodes
            nodes += 1
            if nodes > EXACT_NODE_LIMIT:
                return False
            remaining = n - len(chosen)
            if remaining == 0:
                if not balanced(chosen, counts):
                    return False
                chosen_set = set(chosen)
                solution[flow] = chosen[:]
                return flow_search(flow_index + 1, [r for r in available if r not in chosen_set])
            if len(available) - start < remaining:
                return False
            for j, k in enumerate(counts):
                if sum(max(0, low[j] - frequencies[j][v]) for v in range(1, k + 1)) > remaining:
                    return False
            for i in range(start, len(available) - remaining + 1):
                row = available[i]
                if any(frequencies[j][v] >= high[j] for j, v in enumerate(row)):
                    continue
                chosen.append(row)
                for j, v in enumerate(row):
                    frequencies[j][v] += 1
                if choose(i + 1):
                    return True
                chosen.pop()
                for j, v in enumerate(row):
                    frequencies[j][v] -= 1
                if nodes > EXACT_NODE_LIMIT:
                    break
            return False

        return choose(0)

    return solution if flow_search(0, candidates) else None


def fresh_distribution(
    students: list[Student], counts: tuple[int, ...], seed: int,
    excluded: set[tuple[int, ...]] | None = None,
) -> dict[str, tuple[int, ...]]:
    excluded = set(excluded or ())
    capacity = math.prod(counts)
    if len(students) + len(excluded) > capacity:
        raise ToolError(
            f'Недостаточно уникальных комбинаций: студентов {len(students)}, '
            f'доступно {capacity - len(excluded)} (всего {capacity}).'
        )
    groups = defaultdict(list)
    for student in sorted(students, key=lambda s: s.isu_id):
        groups[student.flow].append(student)
    rng = random.Random(seed)

    # Для плотного заполнения одним потоком удобнее строить сбалансированное дополнение.
    if len(groups) == 1 and not excluded and capacity <= 10_000 and len(students) > capacity // 2:
        all_rows = list(product(*(range(1, k + 1) for k in counts)))
        missing = capacity - len(students)
        if missing:
            fictitious = [Student(str(i), '', 'one', '') for i in range(missing)]
            complement = set(fresh_distribution(fictitious, counts, seed).values())
            rows = [row for row in all_rows if row not in complement]
        else:
            rows = all_rows
        rng.shuffle(rows)
        return {student.isu_id: row for student, row in zip(sorted(students, key=lambda s: s.isu_id), rows)}

    for _ in range(RANDOM_ATTEMPTS):
        result = {}
        seen = set(excluded)
        collision = False
        for flow in sorted(groups):
            members = groups[flow]
            columns = []
            for k in counts:
                q, r = divmod(len(members), k)
                column = list(range(1, k + 1)) * q + rng.sample(range(1, k + 1), r)
                rng.shuffle(column)
                columns.append(column)
            for student, row in zip(members, zip(*columns)):
                if row in seen:
                    collision = True
                    break
                seen.add(row)
                result[student.isu_id] = row
            if collision:
                break
        if not collision:
            return result
    if capacity <= EXACT_SPACE_LIMIT:
        solution = _exact(counts, {f: len(m) for f, m in groups.items()}, excluded, rng)
        if solution is not None:
            return {student.isu_id: row for f, members in groups.items() for student, row in zip(members, solution[f])}
    raise ToolError(
        'Не удалось построить равномерное распределение с уникальными комбинациями. '
        'Назначения не изменены. Попробуйте другой seed в config/project.toml '
        'или другой состав студентов; это не доказательство невозможности распределения.'
    )


def combination(record: dict, counts: tuple[int, ...]) -> tuple[int, ...] | None:
    values = record.get('variants', {})
    row = tuple(values.get(str(i)) for i in range(1, len(counts) + 1))
    if all(type(v) is int and 1 <= v <= k for v, k in zip(row, counts)):
        return row
    return None


def _choose_replacement(
    previous: tuple[int | None, ...], counts: tuple[int, ...], uses: list[Counter],
    occupied: set[tuple[int, ...]], rng: random.Random,
) -> tuple[int, ...]:
    choices = []
    for column, k in enumerate(counts):
        values = list(range(1, k + 1))
        rng.shuffle(values)
        values.sort(key=lambda v: uses[column][v])
        choices.append(values)
    invalid = [i for i, (v, k) in enumerate(zip(previous, counts)) if type(v) is not int or not 1 <= v <= k]
    valid = [i for i in range(len(counts)) if i not in invalid]
    # Сначала меняем только отсутствующие номера, затем минимальное число остальных.
    for additional in range(len(valid) + 1):
        for extras in combinations(valid, additional):
            changed = set(invalid) | set(extras)
            options = [choices[i] if i in changed else [previous[i]] for i in range(len(counts))]
            for row in product(*options):
                if row not in occupied:
                    return row
    raise ToolError('Свободных уникальных комбинаций для замены не осталось. Назначения не изменены.')


def reconcile(
    students: list[Student], counts: tuple[int, ...], seed: int, old: dict[str, dict],
    *, fresh: bool = False,
) -> tuple[dict[str, dict], list[str], bool]:
    selected = {s.isu_id for s in students}
    occupied = {row for sid, rec in old.items() if sid not in selected and (row := combination(rec, counts)) is not None}
    if len(students) + len(occupied) > math.prod(counts):
        raise ToolError('Недостаточно уникальных комбинаций с учётом сохранённых назначений остальных студентов.')
    result = {sid: dict(rec) for sid, rec in old.items()}
    changes = []
    strict = fresh or not old
    if strict:
        assigned = fresh_distribution(students, counts, seed, occupied)
    else:
        assigned = {}
        pending = []
        uses_by_flow = defaultdict(lambda: [Counter() for _ in counts])
        for sid, record in old.items():
            if sid not in selected and (row := combination(record, counts)) is not None:
                for j, v in enumerate(row):
                    uses_by_flow[record['flow']][j][v] += 1
        for s in sorted(students, key=lambda s: s.isu_id):
            rec = old.get(s.isu_id, {})
            row = combination(rec, counts)
            if row is None or row in occupied:
                pending.append(s)
                continue
            assigned[s.isu_id] = row
            occupied.add(row)
            for j, v in enumerate(row):
                uses_by_flow[s.flow][j][v] += 1
        rng = random.Random(seed)
        for s in pending:
            rec = old.get(s.isu_id, {}).get('variants', {})
            previous = tuple(rec.get(str(i)) for i in range(1, len(counts) + 1))
            row = _choose_replacement(previous, counts, uses_by_flow[s.flow], occupied, rng)
            assigned[s.isu_id] = row
            occupied.add(row)
            for j, v in enumerate(row):
                uses_by_flow[s.flow][j][v] += 1
            changes.append(f'ИСУ {s.isu_id}: {"новое назначение" if s.isu_id not in old else "автоматическая замена"}: {list(previous)} → {list(row)}')
    for s in students:
        result[s.isu_id] = {
            'name': s.name, 'flow': s.flow, 'group': s.group,
            'variants': {str(i): v for i, v in enumerate(assigned[s.isu_id], 1)},
        }
    chosen = [combination(result[s.isu_id], counts) for s in students]
    if len(set(chosen)) != len(students) or None in chosen:
        raise ToolError('Внутренняя проверка назначений: неуникальные или недопустимые комбинации.')
    if strict:
        for flow in {s.flow for s in students}:
            if not balanced([combination(result[s.isu_id], counts) for s in students if s.flow == flow], counts):
                raise ToolError(f'Внутренняя проверка: неравномерное распределение потока {flow}.')
    return result, changes, strict
