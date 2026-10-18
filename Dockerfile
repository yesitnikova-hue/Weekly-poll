FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
RUN useradd --uid 10001 --create-home bot && mkdir /data && chown bot:bot /data
COPY --chown=bot:bot bot.py .
USER bot
CMD ["python", "bot.py"]
