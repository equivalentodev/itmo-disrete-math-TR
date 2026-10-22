from __future__ import annotations

from collections import Counter
import csv
from itertools import product
import json
import io
from pathlib import Path
import random
import shutil
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from tool.distribution import balanced, fresh_distribution, reconcile
from tool.inputs import Student, ToolError, check_fragment, read_bank, read_students, select_students
from tool.service import run
from tool.storage import read_state, work_lock
from tool.migration import migrate
from tool.__main__ import menu


PROJECT = Path(__file__).resolve().parents[1]


def roster(sizes):
    return [Student(f'{f}-{i:04d}', f'Студент {f} {i}', str(f), f'M{f}01') for f, n in enumerate(sizes, 1) for i in range(n)]


class DistributionTests(unittest.TestCase):
    def assert_distribution(self, students, counts, assigned):
        self.assertEqual(len(assigned), len(students))
        self.assertEqual(len(set(assigned.values())), len(students))
        for flow in {s.flow for s in students}:
            rows = [assigned[s.isu_id] for s in students if s.flow == flow]
            for j, count in enumerate(counts):
                frequencies = Counter(row[j] for row in rows)
                values = [frequencies[k] for k in range(1, count + 1)]
                self.assertLessEqual(max(values) - min(values), 1)

    def test_actual_sizes_balanced_across_flows_and_globally_unique(self):
        students = roster([113, 112, 110])
        counts = (9, 7, 10, 7, 6, 10, 7, 7, 9, 7)
        assigned = fresh_distribution(students, counts, 20260920)
        self.assert_distribution(students, counts, assigned)
        self.assertEqual(assigned, fresh_distribution(students[::-1], counts, 20260920))

    def test_small_and_dense_spaces(self):
        for counts, sizes in [((2, 2), [2, 2]), ((2, 3), [2, 2, 2]), ((2, 3, 2), [5, 4]), ((2, 3, 2), [11]), ((1, 4), [4]), ((1,), [1])]:
            for seed in range(10):
                with self.subTest(counts=counts, sizes=sizes, seed=seed):
                    students = roster(sizes)
                    self.assert_distribution(students, counts, fresh_distribution(students, counts, seed))

    def test_exact_fallback_not_only_random_retries(self):
        students = roster([3, 3])
        with patch('tool.distribution.RANDOM_ATTEMPTS', 0):
            assigned = fresh_distribution(students, (2, 3), 1)
        self.assert_distribution(students, (2, 3), assigned)

    def test_insufficient_capacity(self):
        with self.assertRaisesRegex(ToolError, 'Недостаточно'):
            fresh_distribution(roster([5]), (2, 2), 1)

    def test_preserve_rename_reorder_and_new_student(self):
        students = roster([4, 3])
        counts = (3, 3, 2)
        old, _, _ = reconcile(students, counts, 5, {})
        renamed = [Student(s.isu_id, 'Новое имя', s.flow, s.group) for s in students[::-1]]
        added = Student('extra', 'Новый', '1', 'M101')
        new, changes, strict = reconcile(renamed + [added], counts, 5, old)
        for s in students:
            self.assertEqual(new[s.isu_id]['variants'], old[s.isu_id]['variants'])
            self.assertEqual(new[s.isu_id]['name'], 'Новое имя')
        self.assertFalse(strict)
        self.assertEqual(len(changes), 1)
        self.assertEqual(len({tuple(r['variants'].values()) for r in new.values()}), 8)

    def test_missing_variant_changes_only_required_component(self):
        s = roster([1])[0]
        old = {s.isu_id: {'name': s.name, 'flow': s.flow, 'group': s.group, 'variants': {'1': 3, '2': 2}}}
        new, changes, strict = reconcile([s], (2, 3), 1, old)
        self.assertIn(new[s.isu_id]['variants']['1'], (1, 2))
        self.assertEqual(new[s.isu_id]['variants']['2'], 2)
        self.assertEqual(len(changes), 1)
        self.assertFalse(strict)

    def test_duplicate_repair_does_not_redistribute_everyone(self):
        students = roster([3])
        old = {s.isu_id: {'name': s.name, 'flow': s.flow, 'group': s.group, 'variants': {'1': 1, '2': 1 if i < 2 else 2}} for i, s in enumerate(students)}
        new, changes, _ = reconcile(students, (2, 2), 1, old)
        self.assertEqual(len(changes), 1)
        self.assertEqual(new[students[0].isu_id]['variants'], old[students[0].isu_id]['variants'])
        self.assertEqual(new[students[2].isu_id]['variants'], old[students[2].isu_id]['variants'])

    def test_fresh_subset_keeps_nonselected_and_avoids_their_combinations(self):
        students = roster([2, 2])
        old, _, _ = reconcile(students, (2, 3), 1, {})
        new, _, _ = reconcile(students[:2], (2, 3), 20, old, fresh=True)
        for s in students[2:]:
            self.assertEqual(new[s.isu_id], old[s.isu_id])
        self.assertEqual(len({tuple(r['variants'].values()) for r in new.values()}), 4)


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='типовики с пробелом-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        shutil.copytree(PROJECT / 'templates', self.root / 'templates')
        (self.root / 'config').mkdir()
        (self.root / 'config/project.toml').write_text(
            'course="Дискретная математика"\nterm="Осень 2026"\nperiod="2026-autumn"\nseed=123\n', encoding='utf-8'
        )

    def bank(self, counts=(3, 3), name='TR1', image=False):
        for n, k in enumerate(counts, 1):
            directory = self.root / 'TR' / name / str(n)
            directory.mkdir(parents=True)
            for v in range(1, k + 1):
                (directory / f'{v}.tex').write_text(
                    f'\\question\nУсловие {n}/{v}, $A=\\{{1,2\\}}$.\n'
                    + ('\\includegraphics{pic/a.png}\n' if image and n == v == 1 else '')
                    + 'Автор -- Автор\n', encoding='utf-8'
                )
        if image:
            path = self.root / 'TR' / name / 'pic'
            path.mkdir()
            (path / 'a.png').write_bytes(b'image bytes for packaging test')

    def csv(self, students=None):
        path = self.root / 'work/students/список студентов.csv'
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('w', encoding='utf-8-sig', newline='') as file:
            writer = csv.writer(file)
            writer.writerow(['ИСУ ID', 'ФИО', 'Поток', 'Группа', 'Лишняя колонка'])
            for s in students or roster([3, 2]):
                writer.writerow([s.isu_id, s.name, s.flow, s.group, 'не используется'])
        return path

    def state_path(self):
        return self.root / 'work/assignments/2026-autumn/TR1/current.json'


class InputTests(Fixture):
    def test_csv_bom_selection_and_no_hardcoded_group_filter(self):
        students = read_students(self.csv())
        self.assertEqual(len(students), 5)
        self.assertEqual(len(select_students(students, 'flows', ('1',))), 3)
        self.assertEqual(len(select_students(students, 'groups', ('M201',))), 2)

    def test_duplicate_ids_and_empty_selection(self):
        students = roster([1]) * 2
        with self.assertRaisesRegex(ToolError, 'повтор ИСУ'):
            read_students(self.csv(students))
        with self.assertRaisesRegex(ToolError, 'ни одного'):
            select_students(roster([1]), 'flows', ('99',))

    def test_variable_task_count_and_ignored_files(self):
        self.bank((1,) * 11, name='TR2')
        path = self.root / 'TR/TR2'
        (path / '.DS_Store').write_bytes(b'ignored')
        (path / '1/notes.txt').write_text('ignored')
        self.assertEqual(len(read_bank(self.root, 'TR2').tasks), 11)

    def test_gap_not_renumbered_automatically(self):
        self.bank()
        path = self.root / 'TR/TR1/1/2.tex'
        path.unlink()
        with self.assertRaisesRegex(ToolError, 'номера должны идти подряд'):
            read_bank(self.root, 'TR1')
        self.assertFalse(path.exists())

    def test_fragment_errors_and_comments(self):
        check_fragment('% \\question ignored\n\\question\n$A=\\{1\\}$ % { ignored\n', 'fixture')
        for text in ('text', '\\question\n\\begin{itemize}\n', '\\question\n$x', '\\question\n{'):
            with self.subTest(text=text), self.assertRaises(ToolError):
                check_fragment(text, 'fixture')

    def test_missing_or_external_image(self):
        self.bank(image=True)
        (self.root / 'TR/TR1/pic/a.png').unlink()
        with self.assertRaisesRegex(ToolError, 'отсутствует рисунок'):
            read_bank(self.root, 'TR1')
        path = self.root / 'TR/TR1/1/1.tex'
        path.write_text('\\question\n\\includegraphics{../../secret.png}\n', encoding='utf-8')
        with self.assertRaisesRegex(ToolError, 'pic/'):
            read_bank(self.root, 'TR1')


class ServiceTests(Fixture):
    def test_menu_review_and_exit(self):
        self.bank()
        with patch('builtins.input', side_effect=['1', '1', '0']), patch('sys.stdout', new_callable=io.StringIO):
            menu(self.root)
        self.assertEqual(len(list((self.root / 'work/exports/TR1').glob('*/*.zip'))), 1)

    def test_eleven_questions_and_dynamic_assignment_csv(self):
        self.bank((2,) * 11, name='TR2')
        result = run(self.root, 'TR2', kind='students', csv_path=self.csv())
        with (result.directory / 'assignments.csv').open(encoding='utf-8-sig', newline='') as file:
            rows = list(csv.reader(file))
        self.assertEqual(len(rows[0]), 15)
        self.assertEqual(rows[0][-1], 'Задание 11')
        with zipfile.ZipFile(result.archive) as z:
            self.assertIn('Типовой расчёт 2', z.read('flow_1.tex').decode())
            self.assertEqual(z.read('flow_1.tex').decode().count('\\question'), 33)

    def test_review_archive_compact_all_variants_and_images(self):
        self.bank(image=True)
        result = run(self.root, 'TR1')
        with zipfile.ZipFile(result.archive) as z:
            self.assertEqual(set(z.namelist()), {'dump.tex', 'README.txt', 'pic/a.png'})
            text = z.read('dump.tex').decode()
            self.assertEqual(text.count('\\question'), 6)
            self.assertIn('\\begin{questions}\n\\setcounter{question}{1}\n\\question', text)
            self.assertNotIn('\\newpage', text)
            self.assertNotIn('Проверка:', text)
            self.assertNotIn('\\textbf{ФИО:}', text)
        self.assertFalse(self.state_path().exists())

    def test_students_flows_and_exact_fragments(self):
        self.bank(image=True)
        path = self.csv()
        result = run(self.root, 'TR1', kind='students', csv_path=path)
        state = json.loads(self.state_path().read_text(encoding='utf-8'))
        with zipfile.ZipFile(result.archive) as z:
            self.assertEqual(set(z.namelist()), {'flow_1.tex', 'flow_2.tex', 'README.txt', 'pic/a.png'})
            for s in read_students(path):
                doc = z.read(f'flow_{s.flow}.tex').decode()
                self.assertEqual(doc.count(f'\\textbf{{ФИО:}} & {s.name}\\\\'), 1)
                for n, variant in state['records'][s.isu_id]['variants'].items():
                    self.assertIn(f'Условие {n}/{variant}', doc)
        self.assertEqual(result.flows, {'1': 3, '2': 2})

    def test_text_change_keeps_assignments_and_versions(self):
        self.bank()
        path = self.csv()
        first = run(self.root, 'TR1', kind='students', csv_path=path)
        old = json.loads(self.state_path().read_text(encoding='utf-8'))['records']
        file = self.root / 'TR/TR1/1/1.tex'
        file.write_text('\\question\nИсправленное условие.\n', encoding='utf-8')
        second = run(self.root, 'TR1', kind='students', csv_path=path)
        self.assertNotEqual(first.archive, second.archive)
        self.assertTrue(first.archive.is_file())
        self.assertEqual(json.loads(self.state_path().read_text(encoding='utf-8'))['records'], old)
        self.assertEqual(json.loads((second.directory / 'assignments.json').read_text(encoding='utf-8'))['records'], old)

    def test_groups_filter_still_outputs_flows(self):
        self.bank()
        result = run(self.root, 'TR1', kind='students', csv_path=self.csv(), selection='groups', values=('M201',))
        with zipfile.ZipFile(result.archive) as z:
            self.assertIn('flow_2.tex', z.namelist())
            self.assertNotIn('flow_1.tex', z.namelist())
        self.assertEqual(result.students, 2)

    def test_redistribution_saves_history_without_zip(self):
        self.bank()
        path = self.csv()
        first = run(self.root, 'TR1', kind='students', csv_path=path)
        old = (first.directory / 'assignments.json').read_bytes()
        result = run(self.root, 'TR1', kind='redistribute', csv_path=path, seed=555)
        self.assertIsNone(result.archive)
        self.assertEqual(json.loads(self.state_path().read_text())['seed'], 555)
        self.assertEqual((first.directory / 'assignments.json').read_bytes(), old)

    def test_zip_failure_preserves_state_and_previous_release(self):
        self.bank()
        path = self.csv()
        first = run(self.root, 'TR1', kind='students', csv_path=path)
        old = self.state_path().read_bytes()
        with patch('tool.service.verify_archive', side_effect=ToolError('test ZIP failure')):
            with self.assertRaisesRegex(ToolError, 'test ZIP failure'):
                run(self.root, 'TR1', kind='students', csv_path=path)
        self.assertEqual(self.state_path().read_bytes(), old)
        self.assertTrue(first.archive.exists())
        self.assertEqual(len(list(first.directory.parent.iterdir())), 1)

    def test_state_commit_failure_rolls_back_new_release(self):
        self.bank()
        path = self.csv()
        first = run(self.root, 'TR1', kind='students', csv_path=path)
        old = self.state_path().read_bytes()
        with patch('tool.service.atomic_json', side_effect=OSError('test state failure')):
            with self.assertRaisesRegex(OSError, 'test state failure'):
                run(self.root, 'TR1', kind='students', csv_path=path)
        self.assertEqual(self.state_path().read_bytes(), old)
        self.assertEqual(len(list(first.directory.parent.iterdir())), 1)

    def test_interrupted_packaging_does_not_publish(self):
        self.bank()
        with patch('tool.service.verify_archive', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                run(self.root, 'TR1')
        self.assertEqual(list((self.root / 'work/exports/TR1').iterdir()), [])
        self.assertFalse((self.root / 'work/.tool.lock').exists())

    def test_concurrent_csv_change_not_published(self):
        self.bank()
        path = self.csv()
        from tool.service import verify_archive
        def changed(archive, project):
            verify_archive(archive, project)
            with path.open('a', encoding='utf-8') as file:
                file.write('\n')
        with patch('tool.service.verify_archive', side_effect=changed):
            with self.assertRaisesRegex(ToolError, 'CSV студентов изменился'):
                run(self.root, 'TR1', kind='students', csv_path=path)
        self.assertFalse(self.state_path().exists())
        self.assertEqual(list((self.root / 'work/exports/TR1').iterdir()), [])

    def test_corrupt_state_not_replaced_and_lock_prevents_parallel_runs(self):
        self.bank()
        path = self.csv()
        current = self.state_path()
        current.parent.mkdir(parents=True)
        current.write_text('{broken', encoding='utf-8')
        with self.assertRaisesRegex(ToolError, 'Повреждён'):
            run(self.root, 'TR1', kind='students', csv_path=path)
        self.assertEqual(current.read_text(), '{broken')
        with work_lock(self.root / 'work'):
            with self.assertRaisesRegex(ToolError, 'Другая операция'):
                run(self.root, 'TR1')


class MigrationTests(Fixture):
    def test_import_legacy_assignments_without_redistribution(self):
        self.bank(image=True)
        students = roster([3, 2])
        csv_path = self.csv(students)
        legacy = self.root / 'work/TR1'
        legacy.mkdir()
        shutil.copytree(self.root / 'TR/TR1', legacy / 'tasks')
        shutil.copytree(self.root / 'TR/TR1/pic', legacy / 'pic')
        shutil.copy2(csv_path, legacy / 'students.csv')
        csv_path.unlink()
        (legacy / 'latex').mkdir()
        records, _, _ = reconcile(students, (3, 3), 1, {})
        with (legacy / 'latex/assignments.csv').open('w', encoding='utf-8', newline='') as file:
            writer = csv.writer(file)
            writer.writerow(['Seed', 1])
            writer.writerow([])
            writer.writerow(['ИСУ ID', 'ФИО', 'Поток', 'Группа', 'Задание 1', 'Задание 2'])
            for s in students:
                writer.writerow([s.isu_id, s.name, s.flow, s.group, *records[s.isu_id]['variants'].values()])
        with zipfile.ZipFile(legacy / 'tr1_overleaf.zip', 'w') as archive:
            archive.writestr('main.tex', 'legacy')
        old_zip = (legacy / 'tr1_overleaf.zip').read_bytes()
        result = migrate(self.root)
        recovery = Path(result['temporary_recovery'])
        self.addCleanup(shutil.rmtree, recovery)
        self.assertFalse(legacy.exists())
        state = json.loads(self.state_path().read_text(encoding='utf-8'))
        self.assertEqual(state['algorithm'], 'legacy-random-v1')
        self.assertEqual(state['records'], records)
        self.assertEqual((Path(result['legacy_export']) / 'tr1_overleaf.zip').read_bytes(), old_zip)
        self.assertTrue(migrate(self.root)['already_migrated'])


if __name__ == '__main__':
    unittest.main()
