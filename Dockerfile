FROM python:3.11-slim

# Робоча директорія всередині контейнера
WORKDIR /app

# Оновлюємо pip та копіюємо залежності
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Копіюємо весь інший код проекту
COPY . .

# Команда для запуску бота
CMD ["python", "bot.py"]