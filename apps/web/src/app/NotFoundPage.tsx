import { navigate } from "./router";

export interface NotFoundPageProps {
  path?: string;
}

export function NotFoundPage({ path }: NotFoundPageProps) {
  return (
    <section className="welcome-card" aria-labelledby="not-found-title">
      <p className="eyebrow">404 · 页面未找到</p>
      <h2 id="not-found-title">未找到所请求的页面</h2>
      <p className="welcome-copy">
        {path ? `未找到路径 “${path}” 对应的业务功能。` : "未找到对应的业务功能。"}
        请检查路径输入是否正确，或返回工作台主页。
      </p>
      <div style={{ marginTop: 24 }}>
        <button
          type="button"
          className="login-button"
          onClick={() => navigate("/")}
        >
          返回工作台主页
        </button>
      </div>
    </section>
  );
}
