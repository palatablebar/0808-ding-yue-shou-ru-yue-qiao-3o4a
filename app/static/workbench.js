import {request} from './client.js';
import {snapshot, history, lineagePanel, attemptPanel} from './view.js';
import {visualize} from './plots.js';

const form = document.querySelector('#source');
const file = document.querySelector('#file');
const profile = document.querySelector('#profile');
const status = document.querySelector('#notice');
const detail = document.querySelector('#review');
const evidence = document.querySelector('#evidence');
const supplementForm = document.querySelector('#supplement');
const supplementFile = document.querySelector('#supplement-file');
const stages = [...document.querySelectorAll('[data-stage]')];
let stage = 'balance';
let report, selected, batch, running = false;

function source() {
  const data = new FormData();
  if (file.files.length) data.append('file',file.files[0]);
  else data.append('sample',profile.value);
  return data;
}

function announce(text, failure = false) {
  status.textContent = text;
  status.dataset.failure = String(failure);
}

function draw() {
  if (!report) return;
  snapshot(report, selected, stage);
  visualize(report, selected);
  for (const button of stages) button.setAttribute('aria-pressed',String(button.dataset.stage===stage));
  document.querySelector('#panel-title').textContent = stages.find(button => button.dataset.stage===stage)?.textContent || '运行对账';
  detail.hidden = !batch;
  supplementForm.hidden = !batch;
  if (batch) {
    document.querySelector('#run-status').textContent = `${batch.id.slice(0,10)} · ${batch.status} · r${batch.revision}`;
    detail.querySelector('[value="APPROVED"]').disabled = !report.balance.releasable || batch.status==='APPROVED';
    detail.querySelector('[value="HELD"]').disabled = batch.status==='APPROVED';
    evidence.href = `/api/runs/${batch.id}/evidence`;
    loadLineage(batch.id);
  }
}

async function loadLineage(runId) {
  try {
    const [chain, attempts] = await Promise.all([
      request(`/api/runs/${runId}/lineage`),
      request(`/api/runs/${runId}/attempts`),
    ]);
    lineagePanel(chain, runId);
    attemptPanel(attempts.items);
  } catch (error) {
    lineagePanel(null);
  }
}

function show(payload, committed = false) {
  batch = committed ? payload : null;
  report = committed ? payload.report : payload;
  detail.elements.note.value = batch?.note || '';
  selected = report.cases?.[0]?.case;
  draw();
}

async function refresh() {
  const data = await request('/api/runs');
  history(data.items, batch?.id);
}

async function perform(action) {
  if (running) return;
  running = true;
  document.body.setAttribute('aria-busy','true');
  announce('处理中…');
  try { await action(); }
  catch (error) { announce(error.message,true); }
  finally { running = false; document.body.removeAttribute('aria-busy'); }
}

function preview(next) {
  return perform(async () => {
    const payload = await request(`/api/stages/${next}`,{method:'POST',body:source()});
    stage = next;
    show(payload);
    announce('阶段预览已更新');
    await refresh();
  });
}

stages.forEach(button => button.addEventListener('click',()=>preview(button.dataset.stage)));
form.addEventListener('submit', event => {
  event.preventDefault();
  perform(async () => {
    const result = await request('/api/runs',{method:'POST',body:source()});
    stage = 'balance'; show(result,true);
    announce('批次已保存'); await refresh();
  });
});
const filename = document.querySelector('#file-name');
profile.addEventListener('change',()=>{file.value=''; filename.textContent=''; preview(stage);});
file.addEventListener('change',()=>{filename.textContent=file.files[0]?.name || ''; preview(stage);});
document.querySelector('#rows').addEventListener('click',event=>{
  const button = event.target.closest('[data-case]');
  if (button) { selected = button.dataset.case; draw(); document.querySelector(`[data-case="${CSS.escape(selected)}"]`).focus(); }
});
document.querySelector('#history').addEventListener('click',event=>{
  const button = event.target.closest('[data-run]');
  if (button) perform(async()=>{
    const result = await request(`/api/runs/${button.dataset.run}`);
    stage='balance'; show(result,true); announce('已载入历史批次'); await refresh();
  });
});
detail.addEventListener('submit',event=>{
  event.preventDefault();
  perform(async()=>{
    const payload={status:event.submitter.value,revision:batch.revision,note:detail.elements.note.value};
    const result=await request(`/api/runs/${batch.id}`,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
    show(result,true); announce(result.status==='APPROVED'?'批次已签发':'批次已暂缓'); await refresh();
  });
});
supplementFile.addEventListener('change',()=>{
  document.querySelector('#supplement-name').textContent = supplementFile.files[0]?.name || '';
});
supplementForm.addEventListener('submit',event=>{
  event.preventDefault();
  if (!supplementFile.files.length) { announce('请先选择补录 CSV', true); return; }
  perform(async()=>{
    const data = new FormData();
    data.append('file', supplementFile.files[0]);
    try {
      const outcome = await request(`/api/runs/${batch.id}/supplements`,{method:'POST',body:data});
      if (outcome.status === 'NO_CHANGE') {
        announce('补录与当前生效版本一致，未产生新版本');
      } else {
        show(outcome.run, true);
        announce(`已生成新版本，重算分组：${outcome.run.report.lineage.affected.join('、') || '无'}`);
      }
    } catch (error) {
      announce(`${error.message}（详见补录尝试定位）`, true);
    }
    await refresh();
    await loadLineage(batch.id);
  });
});
document.querySelector('#lineage-panel').addEventListener('click',event=>{
  const button = event.target.closest('[data-lineage-run]');
  if (button) perform(async()=>{
    const result = await request(`/api/runs/${button.dataset.lineageRun}`);
    stage='balance'; show(result,true); announce('已回读该版本批次'); await refresh();
  });
});
preview(stage);
