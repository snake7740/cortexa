FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY cortexa ./cortexa

ENV CORTEXA_DATA_DIR=/app/data
# ---- 参赛部署固化环境（P0-1：一键起服即合规，勿删）----
# AML 维度 D 治理按 aml schema 生效（默认 novel 会用中文事件词治理英文内容而空转）
ENV CORTEXA_SCENE=aml
# 评测数据脱敏（官方「评测数据不留日志」红线；app 模式会把 query/消息原文落库）
ENV CORTEXA_JOURNAL_MODE=eval
# 领域适配器参赛必须关闭（保持最小面；adapter 关 => RARE-lite 门控关，Search 纯读）
ENV CORTEXA_NOVEL_ADAPTER_ENABLED=false
# 双阶段异步增强参赛关闭（P2-5b 2026-09-06：评测部署最小模型面 + 预算确定性；真实使用可自行开启）
ENV CORTEXA_ASYNC_ENHANCE_ENABLED=false
# 规则分类 LLM 兜底关闭：规则引擎未命中直接落 FACT，Add/Search 全链路零生成模型
# （官方 Q7：全链路零生成模型可申报 N/A——此 ENV 让声明在部署面成立）
ENV CORTEXA_RULE_LLM_FALLBACK=false
# 同会话邻接扩展（InvMem 第1名机制；AMI 离线实测 +4.7；本地官方口径 A/B 250 题 +35 命中 0 回退）
ENV CORTEXA_ADJACENCY_ENABLED=true
# 语义嵌入/鉴权按需经运行环境注入（CORTEXA_EMBEDDING_PROVIDER=openai +
# CORTEXA_OPENAI_API_KEY/BASE_URL；CORTEXA_API_KEY=Memory System Key）

VOLUME ["/app/data"]

EXPOSE 8000

# 容器级存活探针：对齐官方 Health（无鉴权 GET，任意 2xx）；云平台/compose 可直接复用。
# 用标准库而非 curl：slim 镜像不自带 curl，避免为探针多装一层。
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import urllib.request,sys; r=urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4); sys.exit(0 if 200 <= r.status < 300 else 1)"]

CMD ["uvicorn", "cortexa.server:app", "--host", "0.0.0.0", "--port", "8000"]
