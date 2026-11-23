// 知识使用反馈三键（三期 B29 最小版）：对一处引用（章节版本或 Agent 回复）里的每条知识标“有用 / 过时 / 不对”。
// “过时 / 不对”由平台标待复查，进知识专家的抽检队列；附言可选、先过机密级检测，严格档项目不收附言。
import { useEffect, useState } from "react";
import { api } from "../api.js";

const KEYS = [["useful", "有用", "var(--ok)"], ["outdated", "过时", "var(--amber)"], ["wrong", "不对", "var(--crim)"]];

export function useKbFeedback(code, target) {
  const key = JSON.stringify(target || null);
  const [mine, setMine] = useState({});
  const [error, setError] = useState("");
  useEffect(() => {
    setMine({});
    setError("");
    if (!code || !target) return;
    api.kbFeedback(code, target).then((r) => setMine(r.mine || {})).catch(() => {});
  }, [code, key]); // eslint-disable-line react-hooks/exhaustive-deps
  async function pick(doc_id, rating, note = "") {
    setError("");
    try {
      const r = await api.sendKbFeedback(code, { ...target, doc_id, rating, note });
      setMine(r.mine || {});
      return r;
    } catch (e) {
      setError(e.message);
      return null;
    }
  }
  return { mine, pick, error };
}

export function FeedbackKeys({ docId, fb }) {
  const [note, setNote] = useState(null);
  const chosen = fb.mine[docId];
  return (
    <span className="row" style={{ gap: 4, flexWrap: "wrap" }} aria-label={`反馈 ${docId}`}>
      {KEYS.map(([k, label, color]) => (
        <button key={k} type="button" className="btn sm" aria-pressed={chosen === k}
          style={chosen === k ? { borderColor: color, color } : undefined}
          onClick={() => fb.pick(docId, k)}>{label}</button>
      ))}
      {chosen && note === null && <button type="button" className="btn sm" onClick={() => setNote("")}>加一句</button>}
      {note !== null && (
        <>
          <input className="inp" style={{ height: 26, width: 220 }} maxLength={200} value={note}
            placeholder="一句话说明（不写客户信息）" onChange={(e) => setNote(e.target.value)} />
          <button type="button" className="btn sm" disabled={!note.trim()}
            onClick={async () => { if (await fb.pick(docId, chosen, note.trim())) setNote(null); }}>提交</button>
        </>
      )}
    </span>
  );
}
