import { useEffect, useRef, useState } from "react";
import { ApiError } from "../../lib/api/client";
import {
  authorizedRequest,
  getScopeVersion, getSession,
} from "../../lib/auth/session";
import type { Session } from "../../lib/auth/session";

export interface OfficeEvidenceRow {
  id: string;
  version: number;
  sha256: string;
  byte_size: number;
  content_type: string;
  original_name: string;
  source_channel: string;
  created_at: string;
  created_by: string;
}
export interface OfficeReceiptRow extends OfficeEvidenceRow {
  receipt_id: string;
  source_channel: string;
  replayed: boolean;
}
export interface OfficeOFC002Props {
  session?: Session | null;
  baseUrl?: string;
  initialItems?: OfficeEvidenceRow[];
}

export const ACCEPTED_SUFFIXES = [".txt", ".csv", ".pdf", ".xlsx", ".jpg", ".jpeg",
  ".png", ".webp", ".wav", ".mp3", ".ogg"];

export function officeEvidenceSafety(row: OfficeEvidenceRow): string {
  if (!Number.isInteger(row.version) || row.version !== 1 ||
      !/^[0-9a-f]{64}$/.test(row.sha256) ||
      row.byte_size < 1 || row.byte_size > 12 * 1024 * 1024) {
    return "来源版本或哈希异常，暂停使用并联系负责人复核";
  }
  return "原件仅供人工查验；不得当作已批准业务命令";
}

function errorMessage(e: unknown): string {
  return e instanceof ApiError ? e.userMessage : "请求未完成，请核对连接后重试原幂等键。";
}

export async function uploadOfficeFile(file: File, key: string, baseUrl = ""):
  Promise<OfficeReceiptRow> {
  const session = getSession();
  if (!session) throw new ApiError("请先登录");
  return authorizedRequest<OfficeReceiptRow>(
    baseUrl + "/api/v1/office-intake/evidence/file?filename=" +
      encodeURIComponent(file.name),
    {
      method: "POST",
      headers: { "Content-Type": "application/octet-stream" },
      idempotencyKey: key,
      rawBody: file,
    },
  );
}

export async function downloadOfficeEvidence(row: OfficeEvidenceRow, baseUrl = ""):
  Promise<void> {
  const session = getSession();
  if (!session) throw new ApiError("请先登录");
  const response = await authorizedRequest<Response>(
    baseUrl + "/api/v1/office-intake/evidence/" +
      encodeURIComponent(row.id) + "/content",
    {
      rawResponse: true,
    },
  );
  if (response.headers.get("X-Office-Evidence-SHA256") !== row.sha256) {
    throw new ApiError("原件校验信息缺失", {
      userMessage: "来源摘要不一致，禁止使用下载结果",
    });
  }
  const bytes = await response.blob();
  if (bytes.size !== row.byte_size) {
    throw new ApiError("原件大小异常", { userMessage: "下载内容与证据元数据不符" });
  }
  const url = URL.createObjectURL(bytes);
  try {
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = row.original_name;
    anchor.rel = "noopener noreferrer";
      anchor.click();
    } finally { URL.revokeObjectURL(url); }
}

export function OFC002Page({ session, baseUrl = "", initialItems = [] }: OfficeOFC002Props) {
  const [items, setItems] = useState<OfficeEvidenceRow[]>(initialItems);
  const sessionKey = session ? [session.subjectId, session.organizationId,
    session.storeId, session.accessToken].join(":") : "";
  const [loadedScope, setLoadedScope] = useState(sessionKey);
  // A prior employee's source names must disappear in the SAME render as
  // a login/scope switch, not after a subsequent async useEffect.
  const visibleItems = loadedScope === sessionKey ? items : [];
  const [text, setText] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const key = useRef<string | null>(null);
  const invalidate = () => { key.current = null; setMessage(""); };
  const scope = getScopeVersion();

  async function refresh(): Promise<void> {
    const result = await authorizedRequest<{ items: OfficeEvidenceRow[] }>(
      "/api/v1/office-intake/evidence?limit=50", { baseUrl },
    );
    if (getScopeVersion() === scope) {
      setItems(result.items ?? []);
      setLoadedScope(sessionKey);
    }
  }

  useEffect(() => {
    if (!session) { setItems([]); setLoadedScope(""); return; }
    setItems([]);
    let active = true;
    authorizedRequest<{ items: OfficeEvidenceRow[] }>(
      "/api/v1/office-intake/evidence?limit=50", { baseUrl },
    ).then(data => { if (active) {
      setItems(data.items ?? []);
      setLoadedScope(sessionKey);
    } })
      .catch(e => { if (active) setError(errorMessage(e)); });
    return () => { active = false; };
  }, [session?.subjectId, session?.storeId, session?.organizationId, session?.accessToken, baseUrl]);

  async function submit(): Promise<void> {
    if (!session || busy || (!file && !text.trim())) return;
    setBusy(true); setError(""); setMessage("");
    key.current ??= crypto.randomUUID();
    const currentScope = getScopeVersion();
    try {
      const result = file
        ? await uploadOfficeFile(file, key.current, baseUrl)
        : await authorizedRequest<OfficeReceiptRow>(
          "/api/v1/office-intake/evidence/text",
          { baseUrl, method: "POST", headers: { "Idempotency-Key": key.current },
            body: { text } },
        );
      if (getScopeVersion() !== currentScope) return;
      setMessage(result.replayed
        ? "检测到重复提交，已关联原证据；未重复生成任务"
        : "不可变材料收据已保存，等待人工后续处理");
      key.current = null;
      setFile(null); setText("");
      await refresh();
    } catch (e) { if (getScopeVersion() === currentScope) setError(errorMessage(e)); }
    finally { setBusy(false); }
  }

  return <section aria-label="OFC-002材料收件箱">
    <h2>授权材料收件箱</h2>
    <p>仅收件并封存原件；不会自动读取微信、邮件或执行文档中的指令。</p>
    {!session ? <p role="alert">请先登录员工账号。</p> : <>
      <label htmlFor="office-paste">人工粘贴文字</label>
      <textarea id="office-paste" value={text} rows={4} maxLength={131072}
        disabled={busy || file !== null}
        onChange={e => { invalidate(); setText(e.target.value); }} />
      <label htmlFor="office-file">或者人工选择原始文件</label>
      <input id="office-file" type="file"
        accept=".txt,.csv,.pdf,.xlsx,.jpg,.jpeg,.png,.webp,.wav,.mp3,.ogg"
        disabled={busy || text.length > 0}
        onChange={e => { invalidate(); setFile(e.target.files?.[0] ?? null); }} />
      <p>文件上限 12 MiB；文本/表格不执行宏，PDF/音频仅作为隔离附件。</p>
      <button type="button" disabled={busy || (!file && !text.trim())}
        onClick={() => void submit()}>{busy ? "保存中…" : "人工提交并封存"}</button>
      {error && <p role="alert">{error}</p>}
      {message && <p role="status">{message}</p>}
      <h3>本店授权证据（只显示元数据）</h3>
      <ul>{visibleItems.map(row => <li key={row.id}>
        <strong>{row.original_name}</strong>
        <span> · v{row.version} · {row.source_channel} · {row.content_type} · {row.byte_size} 字节</span>
        <div>SHA256：<code>{row.sha256}</code></div>
        <p>{officeEvidenceSafety(row)}</p>
        <button type="button" disabled={busy || !session}
          onClick={() => void downloadOfficeEvidence(row, baseUrl)
            .catch(e => setError(errorMessage(e)))}>授权下载原件</button>
      </li>)}</ul>
    </>}
  </section>;
}
