FROM python:3.10-alpine

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY okxstopi.py .
RUN chmod +x okxstopi.py

CMD ["python", "okxstopi.py"]
