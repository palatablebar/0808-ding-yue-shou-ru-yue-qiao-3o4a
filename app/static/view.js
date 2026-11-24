import {element, pretty} from './client.js';

const labels = {READY:'通过', MISMATCH:'差异待复核', PENDING:'待复核', APPROVED:'已签发', HELD:'已暂缓',
  ORIGINAL:'首发采信', RECALCULATED:'按新口径重算', FROZEN:'沿用已签发结论'};
const headers = ['业务分组', '输入行', '计算值', '参考值', '判定'];

export function badge(status) {
  return element('span', labels[status] || status, {class:'badge', 'data-result':status, title: status});
}

export function snapshot(report, selected, stage) {
  const stats = report.intake;
  document.querySelector('[data-metric="input"]').textContent = stats.input;
  document.querySelector('[data-metric="accepted"]').textContent = stats.accepted;
  document.querySelector('[data-metric="isolated"]').textContent = stats.rejected;
  document.querySelector('[data-metric="cases"]').textContent = report.cases?.length ?? '—';
  const table = document.querySelector('#rows');
  table.replaceChildren();
  const head = element('tr');
  const body = table.createTBody();
  if (stage === 'intake') {
    head.append(...['源行', '记录键', '业务分组', '原因码'].map(title => element('th', title, {scope:'col'})));
    for (const item of stats.isolation) {
      const tr = body.insertRow();
      tr.append(...[item.line,item.record,item.case,item.reason].map(value=>element('td',value)));
    }
    if (!stats.isolation.length) body.insertRow().insertCell().textContent = '当前文件没有隔离行';
  } else {
    head.append(...headers.map(title => element('th', title, {scope:'col'})));
    for (const row of report.cases || []) {
      const tr = body.insertRow();
      const button = element('button',row.case,{type:'button','data-case':row.case,'aria-pressed':String(row.case===selected)});
      tr.insertCell().append(button);
      [row.records.length, row.measure?.value ?? '—', row.measure?.expected ?? '—'].forEach(value=>tr.insertCell().textContent=value);
      tr.insertCell().append(badge(row.status));
      if (row.provenance) tr.insertCell().append(badge(row.provenance.state));
    }
  }
  table.createTHead().append(head);
  const picked = report.cases?.find(row=>row.case===selected);
  document.querySelector('#detail-title').textContent = picked ? `分组 ${picked.case}` : '采信摘要';
  const shown = picked ? picked.measure?.evidence ?? picked.prep?.details ?? {原因:picked.status} : stats.reasons;
  document.querySelector('#detail').textContent = pretty(shown);
  document.querySelector('#counts').textContent = report.balance ? `行数 ${report.balance.accounted}/${report.balance.input} · ${report.balance.ready} 组通过` : `采信 ${stats.accepted} 行 · 隔离 ${stats.rejected} 行`;
}

export function history(items, active) {
  const target = document.querySelector('#history');
  target.replaceChildren();
  for (const run of items) {
    const button = element('button', run.id.slice(0,10), {'data-run':run.id,'aria-pressed':String(run.id===active),type:'button'});
    const item = element('li');
    item.append(button, badge(run.status));
    target.append(item);
  }
  if (!items.length) target.append(element('li','尚无执行批次'));
}

export function lineagePanel(chain, active) {
  const panel = document.querySelector('#lineage-panel');
  panel.hidden = !chain;
  if (!chain) return;
  const list = document.querySelector('#lineage-versions');
  list.replaceChildren();
  for (const version of chain.versions) {
    const li = element('li');
    const button = element('button', `${version.run_id.slice(0,10)} · v${version.depth}`, {
      type:'button', 'data-lineage-run':version.run_id,
      'aria-pressed':String(version.run_id===active),
    });
    li.append(button, badge(version.status));
    if (version.affected.length) li.append(element('span', `重算 ${version.affected.join('、')}`));
    list.append(li);
  }
  document.querySelector('#lineage-head').textContent =
    `链头 ${chain.head_run_id.slice(0,10)} · 共 ${chain.versions.length} 版`;
}

export function attemptPanel(items) {
  const target = document.querySelector('#supplement-attempts');
  target.replaceChildren();
  if (!items?.length) {
    target.append(element('li','尚无补录尝试'));
    return;
  }
  for (const attempt of items) {
    const li = element('li', `${attempt.result} · ${attempt.reason}`);
    if (attempt.blockers.length) {
      const detail = attempt.blockers
        .map(b => b.participants
          ? `${b.record}: ${b.participants.map(p => `行${p.line}=${p.mrr_cents}`).join(' / ')}`
          : `行${b.line} ${b.record} ${b.reason}`)
        .join('；');
      li.append(element('p', detail));
    }
    target.append(li);
  }
}
