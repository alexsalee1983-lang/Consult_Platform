import { useEffect, useState } from "react";
import { api } from "../api.js";

// 四期 B44：知识专家便利性——对照审核、改完即认可、分组成批认可、我的专长。规则以后端 app/kb/expert_ux.py 为准
const DIFF = { background: "var(--amber-d)", color: "var(--amber)", borderRadius: 2 };
const FLAG = { borderLeft: "3px solid var(--amber)", paddingLeft: 8 };

function Segs({ segs }) {
  return segs.map((s, i) => (s.same ? <span key={i}>{s.text}</span>
    : <mark key={i} style={DIFF} title="与另一侧不一致">{s.text}</mark>));
}

// 对照审核：条目段落与所引依据原文并排，出入处高亮；Agent 意见并列，存疑处标出
export function ComparePanel({ versionId }) {
  const [d, setD] = useState(null);
  const [err, setErr] = useState("");
  useEffect(() => { api.benchCompare(versionId).then(setD).catch((e) => setErr(e.message)); }, [versionId]);
  if (err) return <div className="err" role="alert">{err}</div>;
  if (!d) return <div className="sub">对照加载中…</div>;
  const a = d.agent;
  return (
    <div className="pnl pad" style={{ marginTop: 6, display: "flex", flexDirection: "column", gap: 8 }} aria-label="对照审核">
      {d.flags.length ? (
        <div>
          <div className="sub">只看这 {d.flags.length} 处标出的地方：</div>
          <ul className="lst">{d.flags.map((f) => <li key={f.where} style={FLAG}>{f.reason}</li>)}</ul>
        </div>
      ) : <div className="sub">没有标出的地方：引用与原文对得上，Agent 也没提问题，扫一眼即可。</div>}
      {d.citations.length > 0 && (
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8 }}>
          <div className="sub">条目里写的</div><div className="sub">依据原文</div>
          {d.citations.map((c, i) => [
            <div key={`e${i}`} style={c.flag ? FLAG : undefined}>
              <div className="mono sub">{c.document} {c.clause}{c.flag && <> <span className="chip c-a">{c.flag}</span></>}</div>
              <div style={{ whiteSpace: "pre-wrap" }}><Segs segs={c.entry_segments} /></div>
            </div>,
            <div key={`s${i}`}>
              <div className="mono sub">{c.found ? `${c.src_doc_id} ${c.ref_version}` : "库里没有"}</div>
              <div style={{ whiteSpace: "pre-wrap" }}>
                {c.catalog ? <span className="sub">标准只收条款号和标题，没有正文</span> : <Segs segs={c.source_segments} />}
              </div>
            </div>,
          ])}
        </div>
      )}
      <div>
        <div className="sub">知识审核 Agent 的意见{a.confidence ? `（把握 ${a.confidence}）` : ""}{a.flagged ? " · 存疑" : ""}</div>
        {a.reviewed ? (
          <>
            {a.basis && <div>{a.basis}</div>}
            {a.issues.length > 0 && <ul className="lst">{a.issues.map((x) => <li key={x} style={FLAG}>{x}</li>)}</ul>}
          </>
        ) : <div className="sub">Agent 还没出意见。</div>}
      </div>
    </div>
  );
}

// 修改后认可：在线改完即认可，另存为专家版本、署名认可人；原稿留作历史
export function ReviseBox({ card, onDone }) {
  const [body, setBody] = useState(card.body);
  const [ack, setAck] = useState(false);
  const [need, setNeed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [started] = useState(() => Date.now());
  const submit = async () => {
    setBusy(true);
    setErr("");
    try {
      const r = await api.benchRevise(card.version_id, { body, deid_ack: ack, elapsed_ms: Date.now() - started });
      onDone(card.version_id, `已按你改的认可（第 ${r.version} 版，认可人 ${r.approved_by}）`);
    } catch (e) {
      setErr(e.message);
      if (e.status === 409) setNeed(true);
    } finally {
      setBusy(false);
    }
  };
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 6, marginTop: 6 }} aria-label="改完即认可">
      <span className="sub">直接改正文，交上去即以你的名字认可；原稿留作历史版本。</span>
      <textarea className="inp" rows={12} value={body} onChange={(e) => setBody(e.target.value)} />
      {need && (
        <label className="row" style={{ gap: 4 }}>
          <input type="checkbox" checked={ack} onChange={(e) => setAck(e.target.checked)} />
          <span className="sub">我改的文字不是客户信息，照原样交</span>
        </label>
      )}
      <div className="row"><button className="btnp sm" disabled={busy || body.trim() === card.body.trim()} onClick={submit}>改完即认可</button></div>
      {err && <div className="err" role="alert">{err}</div>}
    </div>
  );
}

const pct = (x) => `${(x * 100).toFixed(1)}%`;

// 分组、按影响排序、成批认可
export function GroupView({ onChanged }) {
  const [d, setD] = useState(null);
  const [err, setErr] = useState("");
  const [picked, setPicked] = useState({});
  const [msg, setMsg] = useState("");
  const load = () => api.benchGroups().then((g) => {
    setD(g);
    setPicked(Object.fromEntries(g.groups.flatMap((x) => x.items.filter((i) => !i.flagged).map((i) => [i.version_id, true]))));
  }).catch((e) => setErr(e.message));
  useEffect(() => { load(); }, []);
  if (err) return <section className="pnl pad err" role="alert">{err}</section>;
  if (!d) return <section className="pnl pad sub">加载中…</section>;
  const approve = async (g) => {
    const ids = g.items.filter((i) => picked[i.version_id]).map((i) => i.version_id);
    setMsg("");
    try {
      const r = await api.benchBatchApprove({ version_ids: ids });
      setMsg(`${g.label}：认可 ${r.approved.length} 条${r.skipped.length ? `，跳过 ${r.skipped.length} 条（有存疑，请逐条看）` : ""}`);
      load();
      onChanged?.();
    } catch (e) {
      setMsg(e.message);
    }
  };
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      <div className="sub">按产品与章节分组，影响大的在前（认可后知识健康能提升多少）。当前就绪度 {d.readiness == null ? "暂无" : pct(d.readiness)}。有存疑的不能成批认可，请在“逐条看”里处理。</div>
      {msg && <div className="sub" role="status">{msg}</div>}
      {!d.groups.length && <section className="pnl pad sub">没有要你认可的。</section>}
      {d.groups.map((g) => {
        const n = g.items.filter((i) => picked[i.version_id]).length;
        return (
          <section key={`${g.product}/${g.slot}`} className="pnl pad" aria-label={g.label}>
            <div className="sb">
              <h2 className="h2">{g.label} <span className="sub mono">{g.count} 条 · 可成批 {g.clean}</span></h2>
              <span className="row" style={{ gap: 6 }}>
                {g.gain > 0 && <span className="chip c-n">认可后就绪度 +{pct(g.gain)}</span>}
                <button className="btnp sm" disabled={!n || n > d.batch_max} onClick={() => approve(g)}>认可选中的 {n} 条</button>
              </span>
            </div>
            <ul className="lst">
              {g.items.map((i) => (
                <li key={i.version_id} className="row" style={{ gap: 6 }}>
                  <input type="checkbox" disabled={i.flagged} checked={!!picked[i.version_id]}
                    onChange={(e) => setPicked({ ...picked, [i.version_id]: e.target.checked })} aria-label={i.title} />
                  <span className="mono sub">{i.doc_id} v{i.version}</span> {i.title}
                  <span className="chip c-n">{i.kind_plain}</span>
                  {i.gain > 0 && <span className="sub">+{pct(i.gain)}</span>}
                  {i.flagged && <span className="chip c-a">{i.flag_count} 处存疑</span>}
                  {i.assigned && <span className="sub">按专长派给你</span>}
                </li>
              ))}
            </ul>
          </section>
        );
      })}
    </div>
  );
}

// 我的专长：按专长派卡、按工作量均衡；没填或没有匹配的发全体专家
export function ProfilePanel() {
  const [d, setD] = useState(null);
  const [mine, setMine] = useState(null);
  const [msg, setMsg] = useState("");
  useEffect(() => { api.benchProfile().then((p) => { setD(p); setMine(p.mine); }).catch((e) => setMsg(e.message)); }, []);
  if (!d || !mine) return msg ? <div className="err" role="alert">{msg}</div> : null;
  const toggle = (k, v) => setMine({ ...mine, [k]: mine[k].includes(v) ? mine[k].filter((x) => x !== v) : [...mine[k], v] });
  const save = async () => {
    try {
      setMine(await api.benchSaveProfile({ products: mine.products, industries: mine.industries }));
      setMsg("已保存，之后的待审卡按你的专长派");
    } catch (e) {
      setMsg(e.message);
    }
  };
  return (
    <section className="pnl pad" aria-label="我的专长" style={{ display: "flex", flexDirection: "column", gap: 6 }}>
      <div className="sub">擅长的产品</div>
      <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
        {Object.entries(d.options.products).map(([k, name]) => (
          <label key={k} className="row" style={{ gap: 4 }}>
            <input type="checkbox" checked={mine.products.includes(k)} onChange={() => toggle("products", k)} />{name}
          </label>
        ))}
      </div>
      <div className="sub">熟悉的行业（不选 = 不限）</div>
      <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
        {d.options.industries.map((k) => (
          <label key={k} className="row" style={{ gap: 4 }}>
            <input type="checkbox" checked={mine.industries.includes(k)} onChange={() => toggle("industries", k)} />{k}
          </label>
        ))}
      </div>
      <div className="row" style={{ gap: 6 }}><button className="btnp sm" onClick={save}>保存</button>
        {msg && <span className="sub" role="status">{msg}</span>}</div>
      <div className="sub">
        各位专家手上的待审：{d.experts.map((x) => `${x.username} ${x.load}`).join("，") || "暂无"}；
        没有匹配专长、发给全体的 {d.unassigned} 条
      </div>
    </section>
  );
}
