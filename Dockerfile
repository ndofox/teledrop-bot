FROM python:3.10-slim
WORKDIR /app

COPY requirements.txt requirements.txt
RUN pip3 install -r requirements.txt

COPY . .

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 CMD python3 -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.getenv('PORT', '8080') + '/', timeout=3)"

CMD python3 main.py
