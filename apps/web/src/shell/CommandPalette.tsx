import { useEffect, useMemo, useRef, useState } from "react";
import { navigate } from "../app/router";
import { searchCommandObjects, type CommandSearchResult } from "../lib/api/workspace";
import { useWorkspaceState, openWorkbenchTab, selectWorkbenchTab, setLeftSection } from "../workspace/store";
import { setShellMode } from "./shellMode";
import { getPermittedWorkObjects, toRouteWorkbenchTab } from "./workObjects";
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

export function CommandPalette({
  open,
  onClose,
  capabilities,
  modelOffline,
  onCreateQuoteDraft,
}: CommandPaletteProps) {
  const [query, setQuery] = useState("");
  const [selectedIndex, setSelectedIndex] = useState(0);
  const [remoteResult, setRemoteResult] = useState<CommandSearchResult | null>(null);
  const [searching, setSearching] = useState(false);
  const inputRef = useRef<HTMLInputElement | null>(null);
  const ws = useWorkspaceState();

  useEffect(() => {
    if (!open) {
      setQuery("");
      setSelectedIndex(0);
      setRemoteResult(null);
      return;
    }
    const timer = setTimeout(() => {
      inputRef.current?.focus();
    }, 10);
    return () => clearTimeout(timer);
  }, [open]);

  useEffect(() => {
    if (!open) {
      return;
    }
    let cancelled = false;
    const trimmed = query.trim();
    const timer = setTimeout(() => {
      if (!trimmed && Object.keys(ws.artifactsById).length === 0) {
        return;
      }
      setSearching(true);
      void searchCommandObjects(trimmed)
        .then((res) => {
          if (!cancelled) {
            setRemoteResult(res);
          }
        })
        .catch(() => {
          if (!cancelled) {
            setRemoteResult({ products: [], artifacts: [], degraded: true });
          }
        })
        .finally(() => {
          if (!cancelled) {
            setSearching(false);
          }
        });
    }, 150);

    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [open, query, ws.artifactsById]);

  const entries = useMemo<PaletteEntry[]>(() => {
    const q = query.trim().toLowerCase();
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
            navigate(mod.path);
            openWorkbenchTab(toRouteWorkbenchTab(mod));
            onClose();
          },
        });
      }
    }

    // 2. Deterministic offline-capable quick commands (capability filtered)
    if (capabilities.includes("sales.read") || capabilities.includes("sales.draft")) {
      const label = "新建 L1 报价草稿 Artifact";
      if (!q || `${label} quote 报价 草稿`.toLowerCase().includes(q)) {
        list.push({
          id: "cmd:new-quote-draft",
          group: "确定性快捷动作",
          label,
          hint: "L1 共编草稿（模型离线可用）",
          run: () => {
            onCreateQuoteDraft();
            onClose();
          },
        });
      }
    }

    // 3. Cached / open Artifacts
    for (const art of Object.values(ws.artifactsById)) {
      const text = `${art.title} ${art.kind} ${art.id}`.toLowerCase();
      if (!q || text.includes(q)) {
        list.push({
          id: `local-art:${art.id}`,
          group: "工作成果 Artifact",
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
            onClose();
          },
        });
      }
    }

    // 4. Remote backend artifacts & catalog products
    for (const art of remoteResult?.artifacts ?? []) {
      if (ws.artifactsById[art.id]) continue;
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
          onClose();
        },
      });
    }

    for (const prod of remoteResult?.products ?? []) {
      list.push({
        id: `prod:${prod.id}`,
        group: "商品目录结果 (L0)",
        label: `${prod.name} (${prod.sku})`,
        hint: "点击创建关联报价草稿或定位商品",
        run: () => {
          if (capabilities.includes("sales.read") || capabilities.includes("sales.draft")) {
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
          onClose();
        },
      });
    }

    // 5. Workbench shell commands
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
          onClose();
        },
      },
      {
        id: "shell:legacy",
        group: "工作台视图",
        label: "切换到经典单页模式 (?shell=legacy)",
        hint: "秒级回滚切换",
        run: () => {
          setShellMode("legacy");
          onClose();
        },
      },
    ];
    for (const cmd of extraCmds) {
      if (!q || `${cmd.label} ${cmd.hint}`.toLowerCase().includes(q)) {
        list.push(cmd);
      }
    }

    return list;
  }, [capabilities, onClose, onCreateQuoteDraft, query, remoteResult, ws.artifactsById]);

  if (!open) {
    return null;
  }

  const clampedIndex = Math.min(selectedIndex, Math.max(0, entries.length - 1));

  return (
    <div
      className={styles.paletteBackdrop}
      role="presentation"
      onClick={(e) => {
        if (e.target === e.currentTarget) {
          onClose();
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
            onClose();
          } else if (e.key === "ArrowDown") {
            e.preventDefault();
            setSelectedIndex((prev) => Math.min(entries.length - 1, prev + 1));
          } else if (e.key === "ArrowUp") {
            e.preventDefault();
            setSelectedIndex((prev) => Math.max(0, prev - 1));
          } else if (e.key === "Enter" && entries[clampedIndex]) {
            e.preventDefault();
            entries[clampedIndex].run();
          }
        }}
      >
        <div className={styles.panelHeader}>
          <h3 className={styles.panelTitle}>全局命令面板（Ctrl+K）</h3>
          <button type="button" className={styles.modeButton} onClick={onClose}>
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
            setQuery(e.target.value);
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
                onClick={entry.run}
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
