import { businessPermission } from "../lib/auth/businessPermissions";
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";
import { getScopeVersion, subscribe as subscribeSession } from "../lib/auth/session";
import { getCapabilityEpoch, subscribeCapabilities, useCapabilities } from "../lib/auth/capabilities";
import { navigate } from "../app/router";
import { searchCommandObjects, type CommandSearchResult } from "../lib/api/workspace";
import { openWorkbenchTab, selectWorkbenchTab, setLeftSection } from "../workspace/store";
import { getPermittedWorkObjects, openWorkObjectRouteTab } from "./workObjects";
import styles from "../styles/workbench.module.css";

export interface CommandPaletteProps {
  open: boolean;
  onClose: () => void;
  capabilities: string[];
  modelOffline: boolean;
  onCreateQuoteDraft: (prefill?: { sku?: string; name?: string; unitPrice?: string }) => void;
}

interface PaletteEntry {
  id: string;
  group: string;
  label: string;
  hint: string;
  run: () => void;
}

// Object identity distinguishes A → B → A, even before the debounce or effects run.
type SearchInput = { text: string };
type SearchScope = {
  input: SearchInput;
  session: number;
  epoch: number;
};
type SearchProof = SearchScope & { result: CommandSearchResult };

export function CommandPalette({
  open,
  onClose,
  capabilities,
  modelOffline,
  onCreateQuoteDraft,
}: CommandPaletteProps) {
  const [input, setInput] = useState<SearchInput>({ text: "" });
  const latestInput = useRef(input);
  const active = useRef(false);
  const [selectedIndex, setSelectedIndex] = useState(0);
  const [proof, setProof] = useState<SearchProof | null>(null);
  const inputRef = useRef<HTMLInputElement | null>(null);
  const session = useSyncExternalStore(subscribeSession, getScopeVersion, getScopeVersion);
  const epoch = useSyncExternalStore(subscribeCapabilities, getCapabilityEpoch, getCapabilityEpoch);
  const { status } = useCapabilities();
  const query = input.text;
  const trimmed = query.trim();
  const shouldSearch = open && status === "ready";
  const result = proof?.input === input && proof.session === session &&
    proof.epoch === epoch ? proof.result : null;
  const searching = shouldSearch && !result;

  const isCurrent = useCallback(() =>
    active.current && latestInput.current === input && getScopeVersion() === session &&
    getCapabilityEpoch() === epoch,
  [input, session, epoch]);

  const close = useCallback(() => {
    active.current = false;
    onClose();
  }, [onClose]);

  useLayoutEffect(() => {
    active.current = open;
    if (!open) {
      const cleared = { text: "" };
      latestInput.current = cleared;
      setInput(cleared);
      setSelectedIndex(0);
      setProof(null);
    }
    return () => { active.current = false; };
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const timer = setTimeout(() => inputRef.current?.focus(), 10);
    return () => clearTimeout(timer);
  }, [open]);

  useEffect(() => {
    if (!shouldSearch) return;
    const controller = new AbortController();
    const publish = (result: CommandSearchResult) => {
      if (!controller.signal.aborted && isCurrent()) setProof({ input, session, epoch, result });
    };
    const timer = setTimeout(() => {
      if (!isCurrent()) return;
      void searchCommandObjects(trimmed, controller.signal).then(publish, () => {
        publish({ products: [], artifacts: [], degraded: true });
      });
    }, 150);
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [shouldSearch, trimmed, input, session, epoch, isCurrent]);

  const entries = useMemo<PaletteEntry[]>(() => {
    const q = trimmed.toLowerCase();
    const list: PaletteEntry[] = [];

    // 1. Deterministic permitted module navigation
    const permittedModules = getPermittedWorkObjects(capabilities, { includeHome: true });
    for (const mod of permittedModules) {
      const text = `${mod.title} ${mod.navLabel} ${mod.path} ${mod.category} ${mod.summary}`.toLowerCase();
      if (!q || text.includes(q)) {
        list.push({
          id: `nav:${mod.path}`,
          group: mod.category,
          label: `打开工作对象：${mod.title}`,
          hint: `${mod.path} · ${mod.tier}`,
          run: () => {
            openWorkObjectRouteTab(mod.path);
            close();
          },
        });
      }
    }

    // 2. Deterministic offline-capable quick commands (capability filtered)
    if (capabilities.includes(businessPermission.sales.write)) {
      const label = "新建 L1 报价草稿 Artifact";
      if (!q || `${label} quote 报价 草稿`.toLowerCase().includes(q)) {
        list.push({
          id: "cmd:new-quote-draft",
          group: "确定性快捷动作",
          label,
          hint: "L1 共编草稿（模型离线可用）",
          run: () => {
            onCreateQuoteDraft();
            close();
          },
        });
      }
    }

    // 3. Only the server list can prove that an artifact is visible in this epoch.
    for (const art of result?.artifacts ?? []) {
      list.push({
        id: `remote-art:${art.id}`,
        group: "服务端 Artifact",
        label: art.title,
        hint: `v${art.version} · ${art.status}`,
        run: () => {
          openWorkbenchTab({
            id: `artifact:${art.id}`,
            kind: "artifact",
            title: art.title,
            objectKind: art.kind,
            objectId: art.id,
            artifactId: art.id,
            closable: true,
          });
          close();
        },
      });
    }

    for (const prod of result?.products ?? []) {
      list.push({
        id: `prod:${prod.id}`,
        group: "商品目录结果 (L0)",
        label: `${prod.name} (${prod.sku})`,
        hint: "点击创建关联报价草稿或定位商品",
        run: () => {
          if (capabilities.includes(businessPermission.sales.write)) {
            onCreateQuoteDraft({ sku: prod.sku, name: prod.name });
          } else {
            openWorkbenchTab({
              id: "route:/catalog",
              kind: "route",
              title: "商品查询",
              path: "/catalog",
              closable: true,
            });
            navigate("/catalog");
          }
          close();
        },
      });
    }

    // 4. Workbench shell commands
    const extraCmds: PaletteEntry[] = [
      {
        id: "shell:today",
        group: "工作台视图",
        label: "聚焦 [今日] 首页标签",
        hint: "确定性视图",
        run: () => {
          selectWorkbenchTab("today");
          setLeftSection("today");
          navigate("/");
          close();
        },
      },
    ];
    for (const cmd of extraCmds) {
      if (!q || `${cmd.label} ${cmd.hint}`.toLowerCase().includes(q)) {
        list.push(cmd);
      }
    }

    return list;
  }, [capabilities, close, onCreateQuoteDraft, trimmed, result]);

  if (!open) {
    return null;
  }

  const lastIndex = Math.max(0, entries.length - 1);
  const clampedIndex = Math.min(selectedIndex, lastIndex);
  const activate = (entry: PaletteEntry) => {
    if (isCurrent()) entry.run();
  };

  return (
    <div
      className={styles.paletteBackdrop}
      role="presentation"
      onClick={(e) => {
        if (e.target === e.currentTarget) {
          close();
        }
      }}
    >
      <div
        className={styles.paletteDialog}
        role="dialog"
        aria-modal="true"
        aria-label="全局命令面板"
        data-command-palette="true"
        onKeyDown={(e) => {
          if (e.key === "Escape") {
            e.preventDefault();
            close();
          } else if (e.key === "ArrowDown") {
            e.preventDefault();
            setSelectedIndex((prev) => Math.min(lastIndex, prev + 1));
          } else if (e.key === "ArrowUp") {
            e.preventDefault();
            setSelectedIndex((prev) => Math.max(0, prev - 1));
          } else if (e.key === "Enter" && entries[clampedIndex]) {
            e.preventDefault();
            activate(entries[clampedIndex]);
          }
        }}
      >
        <div className={styles.panelHeader}>
          <h3 className={styles.panelTitle}>全局命令面板（Ctrl+K）</h3>
          <button type="button" className={styles.modeButton} onClick={close}>
            关闭 Esc
          </button>
        </div>

        <input
          ref={inputRef}
          type="search"
          className={styles.paletteInput}
          placeholder="搜索商品、报价单、工作对象路径或动作（离线确定性可用）..."
          aria-label="搜索工作对象或命令"
          value={query}
          onChange={(e) => {
            if (!active.current) return;
            const next = { text: e.target.value };
            latestInput.current = next;
            setInput(next);
            setSelectedIndex(0);
          }}
        />

        <div style={{ fontSize: "12px", color: "var(--muted)" }}>
          {modelOffline
            ? "当前 AI 模型离线，以下命令与只读查询均为 100% 确定性直达。"
            : searching
              ? "正在聚合工作区与商品检索..."
              : `共 ${entries.length} 项可用命令与对象`}
        </div>

        <div className={styles.paletteList} role="listbox" aria-label="命令列表">
          {entries.length === 0 ? (
            <div style={{ padding: "12px", color: "var(--muted)" }}>
              未匹配到有权限的工作对象或动作。
            </div>
          ) : (
            entries.map((entry, idx) => (
              <button
                key={entry.id}
                type="button"
                role="option"
                aria-selected={idx === clampedIndex}
                className={styles.paletteItem}
                style={
                  idx === clampedIndex
                    ? {
                        borderColor: "var(--brand-primary)",
                        background: "var(--brand-primary-subtle)",
                      }
                    : undefined
                }
                onClick={() => activate(entry)}
              >
                <div>
                  <div style={{ fontWeight: 600 }}>{entry.label}</div>
                  <div style={{ fontSize: "12px", color: "var(--muted)" }}>
                    {entry.group}
                  </div>
                </div>
                <span className={styles.shortcutBadge}>{entry.hint}</span>
              </button>
            ))
          )}
        </div>
      </div>
    </div>
  );
}
