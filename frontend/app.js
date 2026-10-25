import {Compartment, EditorState} from '@codemirror/state';
import {EditorView, keymap, lineNumbers, highlightActiveLine, drawSelection, placeholder} from '@codemirror/view';
import {defaultKeymap, history, indentWithTab, isolateHistory, undo, redo, undoDepth} from '@codemirror/commands';
import {searchKeymap, highlightSelectionMatches} from '@codemirror/search';
import {StreamLanguage, syntaxHighlighting, defaultHighlightStyle} from '@codemirror/language';
import {stex} from '@codemirror/legacy-modes/mode/stex';

const $ = id => document.getElementById(id);
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
let csrf, tree = [], current = null, editor, suppressed = false, saveTimer, previewTimer;
let saving = null, navigation = 0, renderGeneration = 0, students = [], assigned = false, busy = false;
let rosterRequest = 0, assignmentRequest = 0;
let previewRunning = null;
let activeTask = null, changingVariant = false;
const editable = new Compartment(), editorStates = new Map();
const actions = {undo:[], redo:[]};
let applyingHistory = false;

function fileActions() {
  $('add-variant').disabled = changingVariant || !activeTask;
  $('delete-variant').disabled = changingVariant || !current;
  $('undo').disabled = changingVariant || !actions.undo.length;
  $('redo').disabled = changingVariant || !actions.redo.length;
}
function setChangingVariant(value) {
  changingVariant = value;
  editor.dispatch({effects: editable.reconfigure([
    EditorState.readOnly.of(!current || value), EditorView.editable.of(Boolean(current) && !value),
  ])});
  fileActions();
}
function rememberEditor() {
  if (current) editorStates.set(current.path, {state: editor.state, revision: current.revision});
}
function recordAction(action) {
  actions.undo.push(action); actions.redo.length = 0; fileActions();
}
function forgetTextHistory(path) {
  for (const stack of Object.values(actions)) {
    for (let i = stack.length - 1; i >= 0; i--) if (stack[i].kind === 'edit' && stack[i].path === path) stack.splice(i, 1);
  }
}
function requestHistory(direction) { historyAction(direction).catch(report); return true; }
async function historyAction(direction) {
  if (changingVariant) return;
  await flush();
  if (changingVariant) return;
  const source = actions[direction], target = actions[direction === 'undo' ? 'redo' : 'undo'];
  const action = source.at(-1);
  if (!action) return;
  setChangingVariant(true);
  try {
    if (action.kind === 'edit') {
      if (current?.path !== action.path) await openFile(action.path, true);
      if (source.at(-1) !== action) throw new Error('Файл изменился извне. Его история правок сброшена.');
      setChangingVariant(false);
      applyingHistory = true;
      let changed;
      try { changed = (direction === 'undo' ? undo : redo)(editor); }
      finally { applyingHistory = false; setChangingVariant(true); }
      if (!changed) throw new Error('История этого файла недоступна.');
      source.pop(); target.push(action);
      await flush(); editor.focus();
    } else {
      const forward = direction === 'redo', removing = (action.kind === 'delete') === forward;
      const from = forward ? 'beforeStates' : 'afterStates', to = forward ? 'afterStates' : 'beforeStates';
      action[from] = taskStates(action.path);
      const result = removing
        ? await api('/api/variants', {method:'DELETE', body:{path:action.path, revision:action.revision}})
        : await api('/api/variants/restore', {method:'POST', body:{backup:action.backup}});
      action.backup = removing ? result.backup : null;
      source.pop(); target.push(action);
      await showVariants(action.path, removing ? result.next : result.path, action[to]);
      if (result.warning) notice(result.warning);
    }
  } finally { setChangingVariant(false); }
}

async function api(url, options = {}) {
  const headers = {'X-CSRF-Token': csrf || '', ...options.headers};
  if (options.body && !(options.body instanceof FormData)) {
    headers['Content-Type'] = 'application/json';
    options.body = JSON.stringify(options.body);
  }
  const response = await fetch(url, {...options, headers});
  const result = await response.json();
  if (!response.ok) throw Object.assign(new Error(result.error || 'Ошибка запроса'), {conflict: result.conflict});
  return result;
}
function notice(message = '') { $('notice').textContent = message; $('notice').hidden = !message; }
function report(error) { notice(error.message || String(error)); }
function status(text, state = 'idle') { $('save-state').textContent = text; $('save-state').dataset.state = state; }
function markStale(message = 'Предпросмотр устарел — есть изменения') {
  $('preview-state').textContent = message;
}
function replaceText(text, state = null) {
  suppressed = true;
  editor.setState(state || makeState(text));
  setChangingVariant(changingVariant);
  editor.dispatch({annotations: isolateHistory.of('after')});
  suppressed = false;
  fileActions();
}
function makeState(doc = '') {
  return EditorState.create({doc, extensions: [
    ...(current ? [lineNumbers(), highlightActiveLine()] : [placeholder('Откройте вариант слева для редактирования.')]),
    editable.of([EditorState.readOnly.of(!current || changingVariant), EditorView.editable.of(Boolean(current) && !changingVariant)]),
    history(), drawSelection(), highlightSelectionMatches(),
    keymap.of([
      {key:'Mod-z', run:()=>requestHistory('undo'), preventDefault:true},
      {key:'Mod-Shift-z', run:()=>requestHistory('redo'), preventDefault:true},
      {key:'Mod-y', run:()=>requestHistory('redo'), preventDefault:true},
      ...defaultKeymap, ...searchKeymap, indentWithTab,
    ]),
    StreamLanguage.define(stex), syntaxHighlighting(defaultHighlightStyle), EditorView.lineWrapping,
    EditorView.contentAttributes.of({'aria-label': 'Исходник LaTeX', spellcheck: 'false'}),
    EditorView.updateListener.of(update => {
      fileActions();
      if (suppressed || !update.docChanged || !current) return;
      if (!applyingHistory) {
        const last = actions.undo.at(-1);
        if (last?.kind === 'edit' && last.path === current.path && undoDepth(update.state) === undoDepth(update.startState)) actions.redo.length = 0;
        else recordAction({kind:'edit', path:current.path});
      }
      current.text = update.state.doc.toString();
      fileActions();
      renderGeneration++;
      clearTimeout(saveTimer); clearTimeout(previewTimer);
      status('Есть изменения'); markStale();
      saveTimer = setTimeout(() => flush().catch(report), 500);
    }),
  ]});
}

function conflict() {
  if (!current) return;
  current.conflict = true;
  $('conflict').hidden = false;
  status('Конфликт версий', 'error'); markStale('Предпросмотр устарел — конфликт версий');
}
async function flush() {
  clearTimeout(saveTimer);
  if (saving) { await saving; return flush(); }
  const file = current;
  if (!file) return;
  if (file.conflict) throw new Error('Сначала разрешите конфликт версий открытого файла.');
  if (file.text === file.savedText) return;
  saving = (async () => {
    while (file === current && file.text !== file.savedText) {
      const text = file.text;
      status('Сохранение…', 'pending');
      try {
        const result = await api('/api/file', {method: 'PUT', body: {path: file.path, text, revision: file.revision}});
        file.revision = result.revision;
        file.savedText = text;
        notice();
        status(file.text === text ? 'Сохранено' : 'Есть изменения');
      } catch (error) {
        if (error.conflict) conflict(); else status('Не удалось сохранить', 'error');
        throw error;
      }
    }
    if (file === current) schedulePreview();
  })();
  try { await saving; } finally { saving = null; }
}

function schedulePreview(delay = 800, quiet = false) {
  clearTimeout(previewTimer);
  const file = current, generation = renderGeneration;
  previewTimer = setTimeout(() => render(file, generation, quiet).catch(error => {
    if (file === current && generation === renderGeneration) { markStale('Не удалось обновить предпросмотр'); report(error); }
  }), delay);
}
async function render(file, generation, quiet = false) {
  if (!file || file !== current || generation !== renderGeneration || file.conflict || file.text !== file.savedText) return;
  const running = {file, generation};
  previewRunning = running;
  try {
  const valid = () => file === current && generation === renderGeneration;
  if (!quiet) markStale($('pdf').hidden ? 'Подготовка предпросмотра…' : 'Обновление… Показана предыдущая версия');
  let result = await api('/api/previews', {method: 'POST', body: {path: file.path, revision: file.revision}});
  while (valid() && ['queued', 'compiling'].includes(result.status)) {
    markStale(`${result.status === 'queued' ? 'В очереди' : 'Компиляция'}…${$('pdf').hidden ? '' : ' Показана предыдущая версия'}`);
    await pause(500);
    if (!valid()) return;
    result = await api(`/api/previews/${result.key}`);
  }
  if (!valid()) return;
  if (result.status === 'superseded') { markStale('Текст обновлён в другой вкладке. Ожидание актуальной версии.'); return; }
  $('diagnostics').hidden = result.status === 'success';
  if (result.status === 'success') {
    const url = `/api/previews/${result.key}/pdf`;
    if ($('pdf').dataset.key !== result.key) {
      $('pdf').src = url + '#view=FitH';
      $('pdf').dataset.key = result.key;
    }
    $('pdf').hidden = false; $('preview-empty').hidden = true;
    markStale('');
  } else {
    $('compile-error').textContent = `${result.line ? `Строка ${result.line}: ` : ''}${result.error}`;
    $('compile-log').textContent = result.log || '';
    markStale($('pdf').hidden ? 'Ошибка компиляции' : 'Ошибка компиляции · показана устаревшая версия');
    $('preview-empty').hidden = true;
  }
  } finally {
    if (previewRunning === running) previewRunning = null;
  }
}

function treeView() {
  const open = new Set([...$('tree').querySelectorAll('details[open]')].map(el => el.dataset.id));
  $('tree').replaceChildren();
  for (const bank of tree) {
    const folder = document.createElement('details'); folder.dataset.id = bank.name;
    folder.open = open.has(bank.name) || current?.path.startsWith(bank.name + '/');
    const summary = document.createElement('summary'); summary.textContent = `Типовик ${bank.name.slice(2)}`; folder.append(summary);
    for (const task of bank.tasks) {
      const group = document.createElement('details'); group.dataset.id = `${bank.name}/${task.number}`;
      group.open = open.has(group.dataset.id) || current?.path.startsWith(group.dataset.id + '/');
      const label = document.createElement('summary'); label.textContent = `Задание ${task.number}`; group.append(label);
      const variants = document.createElement('div'); variants.className = 'variants';
      for (const variant of task.variants) {
        const path = `${bank.name}/${task.number}/${variant}.tex`, button = document.createElement('button');
        button.textContent = `Вариант ${variant}`; button.dataset.path = path;
        button.classList.toggle('active', current?.path === path);
        if (current?.path === path) button.setAttribute('aria-current', 'page');
        button.onclick = () => openFile(path).catch(report);
        variants.append(button);
      }
      if (!task.variants.length) {
        const button = document.createElement('button'); button.textContent = 'Добавить вариант';
        button.onclick = () => createVariant(bank.name, task.number).catch(report);
        variants.append(button);
      }
      group.append(variants); folder.append(group);
    }
    $('tree').append(folder);
  }
}
async function refreshTree() { tree = (await api('/api/tree')).tree; treeView(); }
async function openFile(path, internal = false) {
  if (changingVariant && !internal) return;
  if (current?.path === path) return;
  const sequence = ++navigation;
  await flush();
  const file = await api(`/api/file?path=${encodeURIComponent(path)}`);
  if (sequence !== navigation) return;
  if (current && current.text !== current.savedText) { await flush(); if (sequence !== navigation) return; }
  rememberEditor();
  current = {...file, savedText: file.text, conflict: false};
  const [bank, number] = path.split('/'); activeTask = {bank, number:Number(number)};
  renderGeneration++;
  const cached = editorStates.get(path);
  if (cached && cached.revision !== file.revision) forgetTextHistory(path);
  replaceText(file.text, cached?.revision === file.revision ? cached.state : null);
  $('filename').textContent = 'TR/' + path; status('Сохранено'); $('conflict').hidden = true;
  fileActions();
  $('pdf').hidden = true; $('pdf').removeAttribute('src'); delete $('pdf').dataset.key;
  $('diagnostics').hidden = true;
  $('preview-empty').hidden = false; $('preview-empty').textContent = 'Подготовка предпросмотра…';
  treeView(); notice(); schedulePreview(0);
}
async function externalCheck() {
  if (!current || saving || changingVariant || document.hidden || current.conflict) return;
  const file = current, revision = file.revision;
  try {
    const latest = await api(`/api/file?path=${encodeURIComponent(file.path)}`);
    if (file !== current || file.revision !== revision || saving) return;
    if (latest.revision === revision) {
      // Only the open file: cache lookup also notices changed images/templates.
      if (!previewRunning && file.text === file.savedText) schedulePreview(0, true);
      return;
    }
    if (file.text !== file.savedText) { conflict(); return; }
    Object.assign(file, latest, {savedText: latest.text}); renderGeneration++;
    forgetTextHistory(file.path);
    replaceText(latest.text); status('Обновлено извне'); schedulePreview(0);
  } catch (error) { if (file === current) { status('Файл недоступен', 'error'); report(error); } }
}

async function createVariant(bank, number) {
  if (changingVariant) return;
  await flush();
  if (changingVariant) return;
  setChangingVariant(true);
  try {
    const beforeStates = taskStates(`${bank}/${number}/1.tex`);
    const created = await api('/api/variants', {method:'POST', body:{bank, number}});
    recordAction({kind:'create', path:created.path, revision:created.revision, beforeStates});
    await refreshTree(); await openFile(created.path, true);
  } finally { setChangingVariant(false); }
}
async function deleteVariant() {
  if (!current || changingVariant) return;
  await flush();
  if (!current || changingVariant) return;
  const file = current, task = {...activeTask}, number = Number(file.path.split('/')[2].slice(0, -4));
  if (!window.confirm(`Удалить вариант ${number} задания ${task.number}? Следующие варианты получат номера на единицу меньше. Удаление можно отменить кнопкой «Назад».`)) return;
  setChangingVariant(true);
  try {
    const beforeStates = taskStates(file.path);
    const result = await api('/api/variants', {method:'DELETE', body:{path:file.path, revision:file.revision}});
    recordAction({kind:'delete', path:file.path, revision:file.revision, backup:result.backup, beforeStates});
    await showVariants(file.path, result.next);
  } finally { setChangingVariant(false); }
}

function taskStates(path) {
  rememberEditor();
  const prefix = path.slice(0, path.lastIndexOf('/') + 1);
  return new Map([...editorStates].filter(([name]) => name.startsWith(prefix)));
}
async function showVariants(path, next, states = new Map()) {
  rememberEditor();
  const prefix = path.slice(0, path.lastIndexOf('/') + 1), [bank, number] = path.split('/');
  for (const name of editorStates.keys()) if (name.startsWith(prefix)) editorStates.delete(name);
  for (const [name, state] of states) editorStates.set(name, state);
  ++navigation; ++renderGeneration; clearTimeout(previewTimer);
  current = null; activeTask = {bank, number:Number(number)};
  $('pdf').hidden = true; $('pdf').removeAttribute('src'); delete $('pdf').dataset.key;
  $('diagnostics').hidden = true; $('conflict').hidden = true; markStale('');
  await refreshTree();
  if (next) await openFile(next, true);
  else {
    replaceText(''); $('filename').textContent = `TR/${prefix}`;
    status('Нет вариантов'); $('preview-empty').hidden = false;
    $('preview-empty').textContent = 'Добавьте вариант для предпросмотра.';
  }
  notice();
}

function selectionKind() { return document.querySelector('input[name=selection]:checked').value; }
function selectedValues() { return [...$('selection-values').querySelectorAll('input:checked')].map(el => el.value); }
function selectedStudents() { const kind = selectionKind(), values = selectedValues(); return students.filter(s => kind === 'all' || values.includes(s[kind === 'flows' ? 'flow' : 'group'])); }
function updateCounts() {
  const chosen = selectedStudents(), counts = new Map();
  for (const s of chosen) counts.set(s.flow, (counts.get(s.flow) || 0) + 1);
  $('student-count').textContent = students.length ? `Студентов: ${chosen.length}. ${[...counts].sort(([a],[b]) => a.localeCompare(b, 'ru', {numeric:true})).map(([f,n]) => `Поток ${f}: ${n}`).join(' · ')}` : 'Для именных работ выберите список студентов.';
  $('students-build').disabled = busy || !chosen.length;
  $('distribute').disabled = busy || !chosen.length;
}
function selectionView() {
  const kind = selectionKind(); $('selection-values').replaceChildren(); $('selection-values').hidden = kind === 'all';
  if (kind !== 'all') {
    const values = [...new Set(students.map(s => s[kind === 'flows' ? 'flow' : 'group']))].sort((a,b) => a.localeCompare(b, 'ru', {numeric:true}));
    for (const value of values) {
      const label = document.createElement('label'), input = document.createElement('input');
      input.type = 'checkbox'; input.value = value; input.checked = true; input.onchange = updateCounts;
      label.append(input, ' ' + value); $('selection-values').append(label);
    }
  }
  updateCounts();
}
async function refreshAssignments() {
  const seq = ++assignmentRequest, bank = $('bank').value;
  const result = await api(`/api/assignments?bank=${encodeURIComponent(bank)}`);
  if (seq !== assignmentRequest) return;
  assigned = result.exists; $('distribute').textContent = assigned ? 'Распределить заново' : 'Распределить варианты';
}
async function loadRoster() {
  const seq = ++rosterRequest, name = $('roster').value;
  students = []; selectionView();
  if (name) { const result = await api(`/api/roster?name=${encodeURIComponent(name)}`); if (seq !== rosterRequest) return; students = result.students; }
  selectionView();
}
async function upload(file) {
  if (!file || busy) return;
  const form = new FormData(); form.append('file', file);
  const result = await api('/api/rosters', {method:'POST', body:form});
  const option = new Option(result.name, result.name, true, true); $('roster').add(option);
  await loadRoster(); notice();
}
function textNode(tag, text) { const node = document.createElement(tag); node.textContent = text; return node; }
function exportLink(path, label, primary = false) {
  const link = document.createElement('a');
  link.href = '/api/exports/' + path.split('/').map(encodeURIComponent).join('/');
  link.textContent = label; link.setAttribute('download', '');
  if (primary) link.className = 'download';
  return link;
}
async function build(kind) {
  if (busy) return;
  await flush();
  await refreshAssignments();
  const confirmed = kind !== 'redistribute' || !assigned || window.confirm('Заменить назначения выбранных студентов новым распределением?');
  if (!confirmed) return;
  if (busy) return;
  busy = true; updateCounts(); $('review-build').disabled = true;
  const inputs = [...$('building').querySelectorAll('input,select')];
  inputs.forEach(input => input.disabled = true); $('choose-csv').disabled = true;
  const resultBox = $('build-result'); resultBox.hidden = false; resultBox.dataset.state = 'pending'; resultBox.setAttribute('aria-busy', 'true');
  const progress = {redistribute:'Распределяем варианты…', students:'Собираем архив для студентов…', review:'Собираем типовик для проверки…'};
  resultBox.replaceChildren(textNode('p', progress[kind])); notice();
  resultBox.scrollIntoView({block:'nearest'});
  try {
    const body = {kind, bank:$('bank').value, csv:$('roster').value, selection:selectionKind(), values:selectedValues(), confirmed, pdf:$('compile-pdf').checked};
    const job = await api('/api/builds', {method:'POST', body});
    let result;
    do {
      await pause(500); result = await api(`/api/builds/${job.id}`);
      if (result.message) resultBox.firstChild.textContent = result.message;
    } while (['queued','running'].includes(result.status));
    if (result.status !== 'success') {
      resultBox.dataset.state = 'error';
      const source = result.source;
      resultBox.replaceChildren(textNode('p', source ? `Не удалось собрать PDF: ${source.path}, строка ${source.line}.` : result.error));
      if (result.hint) resultBox.append(textNode('p', result.hint));
      else if (source) resultBox.append(textNode('p', result.detail));
      if (source?.context?.length) {
        const code = textNode('pre', source.context.map(row => `${row.line === source.line ? '→' : ' '} ${row.line}  ${row.text}`).join('\n'));
        code.className = 'build-source'; code.setAttribute('aria-label', 'Фрагмент исходника на момент сборки');
        resultBox.append(code);
      }
      if (source?.path.startsWith('TR/')) {
        const open = textNode('button', 'Открыть задание'); open.type = 'button';
        open.onclick = async () => {
          try {
            await openFile(source.path.slice(3));
            $('edit-mode').click();
            const line = editor.state.doc.line(Math.min(source.line, editor.state.doc.lines));
            editor.dispatch({selection:{anchor:line.from}, scrollIntoView:true}); editor.focus();
          } catch(error) { report(error); }
        };
        resultBox.append(open);
      }
      if (result.log || result.directory) {
        const details = document.createElement('details');
        details.append(textNode('summary', 'Подробности и журнал компиляции'));
        if (result.document) details.append(textNode('p', `В собранном документе: ${result.document}${result.line ? `, строка ${result.line}` : ''}.`));
        if (result.detail) details.append(textNode('p', result.detail));
        if (result.directory) details.append(textNode('p', `Исходники и журнал: ${result.directory}`));
        if (result.log) { const log = textNode('pre', result.log); log.className = 'build-log'; details.append(log); }
        resultBox.append(details);
      }
      return;
    }
    resultBox.dataset.state = 'success';
    resultBox.replaceChildren(textNode('p', kind === 'redistribute' ? 'Распределение сохранено. Теперь можно собрать работы для студентов.' : result.pdfs?.length ? 'Готово. PDF собраны.' : 'Готово. Исходники собраны.'));
    if (result.pdf_archive) resultBox.append(exportLink(result.pdf_archive, 'Скачать все PDF (ZIP)', true));
    if (result.pdfs?.length) {
      const list = document.createElement('ul');
      for (const path of result.pdfs) {
        const item = document.createElement('li'), name = path.split('/').at(-1);
        item.append(exportLink(path, name === 'dump.pdf' ? 'PDF для проверки всех вариантов' : `PDF потока ${name.slice(5, -4)}`)); list.append(item);
      }
      resultBox.append(list);
    }
    if (result.archive) { const paragraph = document.createElement('p'); paragraph.append(exportLink(result.archive, 'Скачать исходники (ZIP)', !result.pdf_archive)); resultBox.append(paragraph); }
    resultBox.append(textNode('p', result.directory));
    if (result.students) resultBox.append(textNode('p', `Студентов: ${result.students}. ${Object.entries(result.flows).map(([f,n])=>`Поток ${f}: ${n}`).join(' · ')}`));
    if (result.changes?.length) { const list = document.createElement('ul'); for (const change of result.changes) list.append(textNode('li',change)); resultBox.append(list); }
    await refreshAssignments();
  } catch(error) { resultBox.dataset.state = 'error'; resultBox.replaceChildren(textNode('p',error.message)); }
  finally {
    resultBox.setAttribute('aria-busy', 'false'); busy = false; updateCounts(); $('review-build').disabled = false;
    inputs.forEach(input => input.disabled = false); $('choose-csv').disabled = false;
    if (!$('building').hidden) resultBox.scrollIntoView({block:'nearest'});
  }
}

async function start() {
  const boot = await api('/api/bootstrap'); csrf = boot.csrf; tree = boot.tree;
  editor = new EditorView({state:makeState(), parent:$('editor')});
  fileActions();
  const modifier = /Mac|iPhone|iPad/.test(navigator.platform) ? '⌘' : 'Ctrl+';
  $('undo').title = `Отменить действие (${modifier}Z)`;
  $('redo').title = `Повторить действие (${modifier}Shift+Z${modifier === 'Ctrl+' ? ' / Ctrl+Y' : ''})`;
  $('undo').onclick = () => requestHistory('undo');
  $('redo').onclick = () => requestHistory('redo');
  treeView();
  for (const bank of tree) $('bank').add(new Option(`Типовик ${bank.name.slice(2)}`,bank.name));
  for (const name of boot.csvs) $('roster').add(new Option(name,name));
  if (boot.csvs.length === 1) $('roster').value=boot.csvs[0];
  $('edit-mode').onclick = () => { $('editing').hidden=false; $('building').hidden=true; $('edit-mode').setAttribute('aria-pressed','true'); $('build-mode').setAttribute('aria-pressed','false'); refreshTree().catch(report); };
  $('build-mode').onclick = async () => { try { await flush(); $('editing').hidden=true; $('building').hidden=false; $('edit-mode').setAttribute('aria-pressed','false'); $('build-mode').setAttribute('aria-pressed','true'); await refreshAssignments(); const fresh=await api('/api/bootstrap'); for(const name of fresh.csvs) if(![...$('roster').options].some(o=>o.value===name)) $('roster').add(new Option(name,name)); } catch(error) { report(error); } };
  $('add-variant').onclick = async () => {
    if (activeTask) createVariant(activeTask.bank, activeTask.number).catch(report);
  };
  $('delete-variant').onclick = () => deleteVariant().catch(report);
  $('load-external').onclick=async()=>{
    const file=current;
    try { const latest=await api(`/api/file?path=${encodeURIComponent(file.path)}`); if(file!==current)return; forgetTextHistory(file.path); Object.assign(file,latest,{savedText:latest.text,conflict:false}); replaceText(latest.text); renderGeneration++; $('conflict').hidden=true; status('Загружена внешняя версия'); notice(); schedulePreview(0); } catch(error){report(error);}
  };
  $('keep-local').onclick=async()=>{
    const file=current;
    try { const latest=await api(`/api/file?path=${encodeURIComponent(file.path)}`); if(file!==current)return; file.revision=latest.revision; file.savedText=latest.text; file.conflict=false; $('conflict').hidden=true; await flush(); notice(); } catch(error){report(error);}
  };
  $('bank').onchange=()=>refreshAssignments().catch(report);
  $('roster').onchange=()=>loadRoster().catch(report);
  $('selection-kind').onchange=selectionView;
  $('upload').onchange=()=>upload($('upload').files[0]).catch(report);
  $('choose-csv').onclick=()=>$('upload').click();
  const zone=$('dropzone');
  zone.ondragover=event=>{event.preventDefault();zone.classList.add('dragging');};
  zone.ondragleave=()=>zone.classList.remove('dragging');
  zone.ondrop=event=>{event.preventDefault();zone.classList.remove('dragging');upload(event.dataTransfer.files[0]).catch(report);};
  $('distribute').onclick=()=>build('redistribute').catch(report);
  $('students-build').onclick=()=>build('students').catch(report);
  $('review-build').onclick=()=>build('review').catch(report);
  let width=45;
  function setWidth(value){width=Math.max(15,Math.min(80,value));$('panes').style.setProperty('--editor-width',width+'%');$('splitter').setAttribute('aria-valuenow',String(Math.round(width)));}
  $('splitter').onpointerdown=event=>{const split=$('splitter');split.setPointerCapture(event.pointerId);split.onpointermove=e=>{if(!split.hasPointerCapture(e.pointerId))return;const rect=$('panes').getBoundingClientRect();if(rect.width)setWidth((e.clientX-rect.left)/rect.width*100);};split.onpointerup=e=>{split.releasePointerCapture(e.pointerId);split.onpointermove=null;};};
  $('splitter').onkeydown=event=>{if(['ArrowLeft','ArrowRight'].includes(event.key)){event.preventDefault();setWidth(width+(event.key==='ArrowRight'?5:-5));}};
  window.addEventListener('beforeunload',event=>{if(current&&(current.text!==current.savedText||saving)){event.preventDefault();event.returnValue='';}});
  window.addEventListener('keydown', event => {
    if ($('editing').hidden || editor.dom.contains(event.target) || event.target.closest('input,textarea,select,[contenteditable=true]') || !(event.metaKey || event.ctrlKey) || event.altKey) return;
    const key = event.code;
    if (key === 'KeyZ' || key === 'KeyY') { event.preventDefault(); requestHistory(key === 'KeyY' || event.shiftKey ? 'redo' : 'undo'); }
  });
  window.addEventListener('focus',()=>{externalCheck();refreshTree().catch(report);});
  setInterval(externalCheck,3000);
  await Promise.all([loadRoster(),refreshAssignments()]);
}
start().catch(report);
