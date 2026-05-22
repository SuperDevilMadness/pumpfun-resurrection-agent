FROM python:3.12-alpine

WORKDIR /app

RUN pip install requests websocket-client

COPY resurrection_agent.py /app/resurrection_agent.py

CMD ["python", "-u", "/app/resurrection_agent.py"]
