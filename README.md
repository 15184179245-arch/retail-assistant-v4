# 零售门店智能经营系统 V4 · 公开源码版

> **面试速览：** Web、Android 和后端均来自本地实际项目。可启动真实 Web 与 API，用合成数据现场登录、查商品和看库存。


V4 是在多端上实现门店工作台、商品与库位检索、销售草稿、到货清点和库存复核的个人软件项目。这里保留本机当前工作树中经筛选的应用源代码，而非重写的展示网页。

| 组件 | 目录 | 技术 |
| --- | --- | --- |
| Web 工作台 | `apps/web/` | TypeScript、React、Vite |
| Android 现场端 | `apps/android/` | Kotlin、Compose、Room、WorkManager |
| API 与数据模型 | `backend/` | Python、FastAPI、SQLAlchemy、Alembic |
| Agent 运行时 | `apps/agent-runtime/` | TypeScript、按配置启用的进程接口 |

## 本地练习

安装 Node.js 24、npm、uv 和 Python 3.12，然后在解压的根目录运行：

```sh
bash scripts/demo.sh
```

首次启动需要下载依赖。终端会打印仅本机能访问的地址（默认 `http://127.0.0.1:8900/catalog`）、练习账号 `demo` 和本次随机密码。登录后搜索 `DEMO-A01` 可查看合成商品。需要换端口时运行 `DEMO_PORT=8901 bash scripts/demo.sh`。按 Ctrl+C 停止并清理临时练习库。练习是实际的 Web 和 API，不启用生产数据库、ERP 或模型服务；不是生产部署。

## 本地复查

**Web**：安装 Node.js 24 和 npm，进入 `apps/web` 后执行 `npm ci`、`npm run typecheck`、`npm run build`；`npm run dev` 启动本地前端。编译通过不等于接入真实业务系统。

**后端**：在仓库根目录执行 `uv sync --locked` 后，运行两组独立使用合成数据的检查：

```sh
uv run pytest -q backend/tests/unit/test_catalog_mapping.py backend/tests/contract/test_search_api.py
```

可用 `uv run uvicorn backend.app.main:app --host 127.0.0.1 --port 8000` 启动本地健康检查；完整业务流程需要自行配置空的本地数据库及测试身份，仓库不附真实账号或数据库。Android 源码单独位于 `apps/android`，本地构建需要 JDK 17、Android SDK 34 与 Gradle 8.7；本次未验证 Android 编译及设备联调。

## 公开版边界

公开版没有原公司的库存、价格、客户、单据、真实店铺配置、账号密钥、运维材料、截图、日志、历史提交或未经审核的本地文件。业务系统的连接地址替换为不可解析的示例；启用本地演示不需要连接原业务系统。外部系统、私有运行环境和依赖第三方原型的编排层不随包提供；本仓库不等同于完整的生产部署包。
