FROM python
ENV PYTHONUNBUFFERED 1
RUN mkdir -p /app
COPY requirements.txt /app
RUN pip install -r /app/requirements.txt
COPY . /app
CMD python /app/test.py
