import { navigate } from "../app/router";
import { openWorkbenchTab, setLeftSection } from "../workspace/store";
import { getPermittedWorkObjects, OBJECT_LIBRARY_CATEGORIES, toRouteWorkbenchTab } from "./workObjects";
import styles from "../styles/workbench.module.css";

export interface ObjectLibraryViewProps {
  capabilities: string[];
}

export function ObjectLibraryView({ capabilities }: ObjectLibraryViewProps) {
  const permitted = getPermittedWorkObjects(capabilities);

  return (
    <section aria-label="五大分类工作对象库" data-object-library="true">
      <div className={styles.panelHeader} style={{ marginBottom: "12px" }}>
        <div>
          <h3 className={styles.panelTitle}>工作对象库（13 业务模块按 5 类收敛）</h3>
          <p style={{ margin: "4px 0 0", fontSize: "13px", color: "var(--muted)" }}>
            依当前身份能力动态过滤；点击任意对象即在右侧工作台开启多标签视图，并同步支持直接 URL 访问。
          </p>
        </div>
        <button
          type="button"
          className={styles.secondaryBtn}
          onClick={() => setLeftSection("today")}
        >
          返回工作台视图
        </button>
      </div>

      {OBJECT_LIBRARY_CATEGORIES.map((category) => {
        const items = permitted.filter((item) => item.category === category);
        if (items.length === 0) {
          return null;
        }
        return (
          <div key={category} style={{ marginBottom: "16px" }}>
            <h4 style={{ margin: "0 0 8px", fontSize: "14px", color: "var(--brand-primary)" }}>
              {category}
            </h4>
            <div className={styles.cardGrid}>
              {items.map((mod) => (
                <article key={mod.path} className={styles.moduleCard}>
                  <div>
                    <div className={styles.panelHeader}>
                      <strong>{mod.title}</strong>
                      <span className={styles.shortcutBadge}>{mod.path}</span>
                    </div>
                    <p style={{ margin: "6px 0 0", fontSize: "12px", color: "var(--muted)" }}>
                      {mod.summary}
                    </p>
                    <p style={{ margin: "4px 0 0", fontSize: "11px", color: "var(--ink-eyebrow)" }}>
                      级别：{mod.tier} · 所需权限：{mod.requiredCapability} ·{" "}
                      {mod.offlineAvailable ? "模型离线可用" : "模型在线推理"}
                    </p>
                  </div>
                  <button
                    type="button"
                    className={styles.primaryBtn}
                    onClick={() => {
                      setLeftSection("today");
                      navigate(mod.path);
                      openWorkbenchTab(toRouteWorkbenchTab(mod));
                    }}
                  >
                    打开工作对象
                  </button>
                </article>
              ))}
            </div>
          </div>
        );
      })}
    </section>
  );
}
