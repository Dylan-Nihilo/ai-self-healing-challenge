import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react';
import Icon from './Icon';

type Check = { id: string; label: string; ok: boolean; detail: string };
type Job = { id: string; title: string; state: string; error: string | null; created_at: string };
type RecordItem = { seq: number; created_at: string; actor: string; title: string; details: Record<string, unknown> };
type Status = { ready: boolean; checks: Check[]; checked_at: string | null; version: string; events: RecordItem[]; jobs: Job[]; agent: { state: string; message: string; summary: string | null; last_run_at: string | null } };
type Document = { id: string; title: string; file: string; text?: string };
type SearchResult = { id: string; title: string; excerpt: string };
const when = (value: string) => new Date(value).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
const message = (value: unknown) => value instanceof Error ? value.message : '操作未完成，请重试。';
const jobNames: Record<string, string> = { queued: '等待处理', working: '正在生成', failed: '归档失败', done: '已完成' };
const agentNames: Record<string, string> = { waiting: '等待自检', not_connected: '等待修复模块', running: '正在自主修复', verifying: '正在核验结果', error: '本轮修复未完成', blocked: '等待补充处理', watching: '持续监测中' };
const tools: Record<string, string> = { read_resource: '读取运行资源', list_snapshots: '查看可用快照', read_snapshot: '读取目录快照', list_manuals: '查询恢复资料', read_manual: '阅读恢复手册', inspect_storage: '检查存储位置', restore_catalog: '恢复资料目录', rebuild_index: '重建检索索引', update_settings: '更新运行配置', reload_worker: '加载工作进程配置', retry_job: '继续处理原有任务', run_self_check: '执行独立功能核验' };

async function api<T>(path: string, body?: unknown, signal?: AbortSignal): Promise<T> {
  let response: Response;
  try { response = await fetch('/api' + path, { signal, ...(body === undefined ? {} : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }) }); }
  catch (error) { if (signal?.aborted) throw error; throw new Error('无法连接工作站，请检查 Docker 服务。'); }
  const value = await response.json();
  if (!response.ok) throw new Error(value.error?.message || `请求失败（${response.status}）`);
  return value as T;
}

export default function App() {
  const [status, setStatus] = useState<Status | null>(null);
  const [error, setError] = useState('');
  const [connectionError, setConnectionError] = useState('');
  const [busy, setBusy] = useState(false);
  const busyRef = useRef(false);
  const sequence = useRef(0);
  const [documents, setDocuments] = useState<Document[]>([]);
  const [libraryError, setLibraryError] = useState('');
  const [query, setQuery] = useState('');
  const [results, setResults] = useState<SearchResult[] | null>(null);
  const [searchError, setSearchError] = useState('');
  const [modal, setModal] = useState<'task' | Document | null>(null);
  const dialog = useRef<HTMLDialogElement>(null);
  const [drillOpen, setDrillOpen] = useState(false);
  const drill = useRef<HTMLDialogElement>(null);
  const catalogReady = status?.checks.find(item => item.id === 'catalog')?.ok;
  const searchReady = status?.checks.find(item => item.id === 'search')?.ok;

  const refresh = useCallback(async (signal?: AbortSignal) => {
    const current = ++sequence.current;
    const value = await api<Status>('/status', undefined, signal);
    if (!signal?.aborted && current === sequence.current) { setStatus(value); setConnectionError(''); }
  }, []);
  useEffect(() => {
    const controller = new AbortController();
    const load = () => refresh(controller.signal).catch(err => { if (!controller.signal.aborted) setConnectionError(message(err)); });
    void load();
    const timer = window.setInterval(load, 2000);
    return () => { controller.abort(); window.clearInterval(timer); };
  }, [refresh]);
  useEffect(() => {
    const controller = new AbortController();
    api<Document[]>('/library', undefined, controller.signal).then(value => { if (!controller.signal.aborted) { setDocuments(value); setLibraryError(''); } })
      .catch(err => { if (!controller.signal.aborted) { setDocuments([]); setLibraryError(message(err)); } });
    return () => controller.abort();
  }, [catalogReady]);
  useEffect(() => { setResults(null); setSearchError(''); }, [searchReady]);
  useEffect(() => {
    if (modal && !dialog.current?.open) dialog.current?.showModal();
    if (!modal && dialog.current?.open) dialog.current?.close();
  }, [modal]);
  useEffect(() => {
    if (drillOpen && !drill.current?.open) drill.current?.showModal();
    if (!drillOpen && drill.current?.open) drill.current?.close();
  }, [drillOpen]);

  async function act(operation: () => Promise<unknown>) {
    if (busyRef.current) return;
    busyRef.current = true; setBusy(true); setError('');
    try { await operation(); await refresh(); }
    catch (err) { setError(message(err)); }
    finally { busyRef.current = false; setBusy(false); }
  }
  async function search(event: FormEvent) {
    event.preventDefault();
    if (!query.trim() || busyRef.current) return;
    await act(async () => {
      setSearchError(''); setResults(null);
      try { setResults(await api<SearchResult[]>('/search?q=' + encodeURIComponent(query.trim()))); }
      catch (err) { setSearchError(message(err)); }
    });
  }
  const openDocument = (id: string) => void act(async () => setModal(await api<Document>('/documents/' + encodeURIComponent(id))));
  const passed = status?.checks.filter(item => item.ok).length || 0;
  const running = status?.agent.state === 'running';

  return <div className="app">
    <header className="topbar"><a className="brand" href="/" aria-label="自愈工作站首页"><img src="/brand.png" alt="万象硅芯" /></a><span className="product-name">自愈工作站</span><span className="edition">工程考核 · 本地环境</span><button className="quiet-button" onClick={() => setModal('task')}><Icon name="book" />任务说明</button></header>
    <main className="workspace">
      {(error || connectionError) && <div className="alert" role="alert">{error || connectionError}<button aria-label="关闭提示" onClick={() => { setError(''); setConnectionError(''); }}><Icon name="close" /></button></div>}
      <section className="intro"><div><div className={`system-state ${status?.ready ? 'healthy' : 'degraded'}`}><i />{status?.ready ? '运行正常' : status?.checks.length ? '恢复模式' : '正在自检'}<span>WORKSTATION {status?.version || '2.1'}</span></div><h1>{status?.ready ? '工作站已恢复。' : status?.checks.length ? '这台工作站还不能正常工作。' : '正在检查工作站。'}</h1><p>{status?.ready ? '资料读取、检索、归档及原有任务均通过核验。持续监测已开启。' : '它自己的功能发生了故障。接入 AI 修复模块，让它自主恢复，并在后续故障出现时继续处理。'}</p></div><div className="check-total"><strong>{passed}<span>/ 4</span></strong><span>功能核验通过</span><button className="text-button" disabled={busy} onClick={() => void act(() => api('/checks', {}))}><Icon name="refresh" width="14" height="14" />立即自检</button></div></section>

      <section className="candidate-entry"><span className="entry-number">你的任务</span><p>实现 <code>solution/agent.py</code> 中的 <code>repair(context, tools)</code>，<br className="wide-break" />让 AI 调查并修复当前工作站。页面、工具和独立自检已经提供。</p><button className="text-button" onClick={() => setModal('task')}>查看修改范围与完成条件<Icon name="arrow" width="15" height="15" /></button></section>

      <section className="health-strip" aria-label="工作站功能状态">{status?.checks.length ? status.checks.map((item, index) => <article className={item.ok ? 'check-pass' : 'check-fail'} key={item.id}><div><span className="check-index">0{index + 1}</span><h2>{item.label}</h2><span className="check-result">{item.ok ? <Icon name="check" width="15" height="15" /> : <span className="failure-mark">!</span>}{item.ok ? '通过' : '失败'}</span></div><p>{item.detail}</p></article>) : <p className="checking" role="status">正在通过真实请求检查资料、检索和归档功能…</p>}</section>

      <div className="work-columns">
        <div className="product-workspace">
          <section className="library-section"><div className="section-title"><h2><Icon name="book" />资料库</h2><span>{documents.length ? `${documents.length} 份原件` : '原件保留在工作站内'}</span></div>{libraryError ? <div className="function-error"><Icon name="file" width="26" height="26" /><div><h3>暂时无法读取资料</h3><p>{libraryError}</p><span>修复目录后，这里会重新显示资料列表。</span></div></div> : documents.length ? <div className="documents">{documents.map(item => <button key={item.id} disabled={busy} onClick={() => openDocument(item.id)}><Icon name="file" /><span><strong>{item.title}</strong><small>{item.file}</small></span><Icon name="arrow" width="15" height="15" /></button>)}</div> : <p className="muted">正在读取资料…</p>}</section>

          <section className="search-section"><div className="section-title"><h2>搜索资料</h2><span>查询标题与正文</span></div><form className="search-form" onSubmit={search}><label className="sr-only" htmlFor="query">搜索资料</label><input id="query" value={query} maxLength={100} onChange={event => setQuery(event.target.value)} placeholder="输入关键词，例如“安全保存”" /><button className="secondary" disabled={busy || !query.trim()}>搜索</button></form>{searchError && <p className="inline-error" role="alert">{searchError}</p>}{results && <div className="search-results">{results.length ? results.map(item => <button key={item.id} disabled={busy} onClick={() => openDocument(item.id)}><strong>{item.title}</strong><p>{item.excerpt}</p></button>) : <p className="muted">没有找到对应资料。</p>}</div>}</section>

          <section className="jobs-section"><div className="section-title"><h2>资料归档</h2><button className="text-button" disabled={busy} onClick={() => void act(() => api('/jobs', {}))}><Icon name="plus" width="14" height="14" />生成资料包</button></div><p className="section-note">由工作进程读取原件并生成 ZIP。已有任务会在恢复后继续处理。</p>{status?.jobs.map(item => <article className={`job job-${item.state}`} key={item.id}><span className="job-icon"><Icon name="file" /></span><div className="job-copy"><strong>{item.title}</strong><small>{item.id.slice(0, 8)} · {when(item.created_at)}</small>{item.error && <p>{item.error}</p>}</div><div className="job-state"><span>{jobNames[item.state] || item.state}</span>{item.state === 'done' && <a href={`/api/jobs/${item.id}/file`}>下载 ZIP<Icon name="external" width="13" height="13" /></a>}</div></article>)}</section>
        </div>

        <aside className="repair-panel"><div className="section-title"><h2><Icon name="tools" />自主修复</h2><span className={running ? 'running-label' : ''}>{running ? '执行中' : '后台监测'}</span></div><h3 className="agent-title">{status ? agentNames[status.agent.state] || '正在检查' : '正在连接'}</h3><p className="agent-message">{status?.agent.message || '读取当前状态…'}</p>{status?.agent.state === 'not_connected' && <div className="agent-entry"><code>solution/agent.py</code><p>在这里接入模型与修复逻辑。保存代码后，下一轮会自动读取；新增依赖需要重建 Docker 镜像。</p></div>}<button className="primary" disabled={busy || running || !!status?.ready} onClick={() => void act(() => api('/repair', {}))}><Icon name="refresh" width="15" height="15" />重新运行修复模块</button>{status?.agent.summary && <details className="last-summary"><summary>上次修复说明</summary><p>{status.agent.summary}</p></details>}<div className="monitor-note"><i /><span>约每 5 秒独立自检<br />{status?.checked_at ? `最近核验 ${when(status.checked_at)}` : '等待第一轮核验'}</span></div>
          <div className="activity-heading"><h3>运行记录</h3><span>{status?.events.length || 0} 条</span></div><div className="activity">{status?.events.slice(0, 12).map(item => <article key={item.seq}><span className={`event-dot actor-${item.actor}`} /><div><div className="event-heading"><strong>{item.title.startsWith('调用 ') ? tools[item.title.slice(3)] || item.title : item.title}</strong><time>{when(item.created_at)}</time></div><span className="event-actor">{item.actor === 'assistant' ? '修复模块' : item.actor === 'checker' ? '独立自检' : item.actor === 'worker' ? '归档进程' : '工作站'}</span><details><summary>查看记录</summary><pre>{JSON.stringify(item.details, null, 2)}</pre></details></div></article>)}</div>
        </aside>
      </div>
      <footer className="footer"><div><strong>验证持续自愈</strong><span>再次引入一处故障，观察已接入的修复模块能否自动恢复。</span></div><button className="secondary" disabled={busy || running || !status?.ready} onClick={() => setDrillOpen(true)}>触发一次故障<Icon name="arrow" width="15" height="15" /></button></footer>
    </main>

    <dialog ref={dialog} className="detail-dialog" onClose={() => setModal(null)} onCancel={() => setModal(null)} aria-labelledby="dialog-title"><div className="dialog-header"><div><span>{modal === 'task' ? '万象硅芯 · AI 研发实习生工程任务' : '资料原件'}</span><h2 id="dialog-title">{modal === 'task' ? '让这台工作站学会自我修复。' : modal?.title}</h2></div><button className="icon-button" aria-label="关闭详情" onClick={() => setModal(null)}><Icon name="close" /></button></div>{modal === 'task' ? <div className="task-text"><h3>你接手的是什么？</h3><p>这是一台具备资料浏览、全文检索和 ZIP 归档功能的工作站。初始版本中，它自己的目录、索引和归档运行环境存在故障，已有任务也没有完成。故障会导致真实请求失败。</p><h3>你要编写什么？</h3><p>在 <code>solution/agent.py</code> 的 <code>repair(context, tools)</code> 中接入真实模型，实现自主诊断、资料使用、工具调用、修复和结果判断。用户不需要替 AI 查日志或逐步选择修复操作。</p><p>维护页面、受限工具、工作进程及独立自检由我们提供。候选人可以修改 <code>solution/**</code>、<code>tests/solution/**</code> 以及交付文档；固定底座和验收器保持原样。详见仓库 <code>docs/BOUNDARY.md</code> 和 <code>docs/CONTRACTS.md</code>。</p><h3>怎样才算完成？</h3><ol><li>实际资料能够读取、搜索，并生成可解包的 ZIP，原件与原有任务完整保留。</li><li>固定自检全部通过，随后再次注入故障时，AI 能自动发现并恢复。</li><li>在相同接口的新环境中继续有效，不依赖写死的目录名、快照 ID 或任务 ID。</li><li>提交可运行代码、真实模型演示、运行说明和关键 Claude/Codex 协作记录。</li></ol><p className="task-footnote">自检通过来自真实功能检查。仅返回“已修复”、手工恢复或更改验收器都不代表完成。初始 AI 模块留空，输入不包含语音。</p><a className="secondary" href="/task.pdf" target="_blank" rel="noreferrer">下载任务书<Icon name="external" width="15" height="15" /></a></div> : modal && <article className="document-text"><pre>{modal.text}</pre></article>}</dialog>

    <dialog ref={drill} className="drill-dialog" onClose={() => setDrillOpen(false)} onCancel={() => setDrillOpen(false)} aria-labelledby="drill-title"><h2 id="drill-title">触发一次恢复演练</h2><p>工作站的一项功能会暂时失败，原始资料和已完成归档会保留。后台监测将调用你的修复模块处理新故障。</p><div className="dialog-actions"><button className="text-button" onClick={() => setDrillOpen(false)}>取消</button><button className="primary" disabled={busy} onClick={() => void act(async () => { await api('/drill', {}); setDrillOpen(false); })}>触发故障</button></div></dialog>
  </div>;
}
