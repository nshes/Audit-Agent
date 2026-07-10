# ============================================================
# Audit Agent — Dockerfile
# 베이스: Ubuntu 24.04 LTS (오케스트레이터 서버 환경과 동일)
# 배포: 오케스트레이터가 git pull 후 docker build 로 이미지 생성
# ============================================================

FROM ubuntu:24.04

# 비대화형 모드 (apt 설치 중 프롬프트 방지)
ENV DEBIAN_FRONTEND=noninteractive

# 1) 시스템 패키지 설치
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3.12 \
    python3.12-venv \
    python3-pip \
    curl \
    && rm -rf /var/lib/apt/lists/*

# 2) 가상환경 생성 (Ubuntu 24.04의 "externally-managed-environment" 정책 대응)
RUN python3.12 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# 3) 작업 디렉토리 설정
WORKDIR /app

# 4) 의존성 설치 (소스보다 먼저 복사 → Docker 레이어 캐시 활용)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 5) 소스 코드 복사
COPY audit_agent.py .
COPY app.py .
COPY agent.yaml .

# 6) 8000 포트 노출
EXPOSE 8000

# 7) 헬스체크 (오케스트레이터가 컨테이너 준비 상태를 확인)
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

# 8) 비루트 사용자로 실행 (보안 강화)
RUN useradd -m -u 10001 appuser
USER appuser

# 9) 서버 기동
CMD ["/opt/venv/bin/uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
