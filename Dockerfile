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
# 领域适配器参赛必须关闭（保持最小面）
ENV CORTEXA_NOVEL_ADAPTER_ENABLED=false
# 语义嵌入/鉴权按需经运行环境注入（CORTEXA_EMBEDDING_PROVIDER=openai +
# CORTEXA_OPENAI_API_KEY/BASE_URL；CORTEXA_API_KEY=Memory System Key）

VOLUME ["/app/data"]

EXPOSE 8000

CMD ["uvicorn", "cortexa.server:app", "--host", "0.0.0.0", "--port", "8000"]
