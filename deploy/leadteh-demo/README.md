# Демо массива сообщений для LEADTEX

Тестовый сценарий получает один JSON-массив и отправляет каждый его элемент отдельным сообщением Telegram. В конструкторе используются два HTTP-блока независимо от количества элементов.

Готовая схема с пояснениями: [LEADTEX-array-loop-scheme.pdf](LEADTEX-array-loop-scheme.pdf).

## Тестовые ответы

- `https://pay.sushi-house-39.ru/leadteh-demo/messages-0.json`
- `https://pay.sushi-house-39.ru/leadteh-demo/messages-7.json`
- `https://pay.sushi-house-39.ru/leadteh-demo/messages-10.json`
- `https://pay.sushi-house-39.ru/leadteh-demo/messages-25.json`

Формат ответа:

```json
{
  "success": true,
  "demo": true,
  "count": 7,
  "items": [
    {
      "id": "demo-1",
      "text": "Тест API: сообщение 1 из 7.\nЭта запись пришла в общем JSON-массиве.",
      "button_text": "Открыть исходный JSON",
      "url": "https://pay.sushi-house-39.ru/leadteh-demo/messages-7.json"
    }
  ]
}
```

## Сценарий LEADTEX

- Сценарий: `ТЕСТ API: массив и HTTP-цикл`, ID `2352423`.
- Блок получения массива: ID `49905282`.
- Блок отправки сообщения: ID `49905288`.
- Прямая тестовая ссылка: `https://app.leadteh.ru/w/giT34`.
- Событие `demo_array_7` оставлено отключённым: событие типа «Любое событие Telegram» нельзя использовать как надёжный фильтр команды в этом боте.

Связи через «Продолжить»: `Старт -> 00 Запуск демо -> 01 API: получить массив`. После получения массива управление передаётся только через `goToBlock()`.

### Блок 01: получить массив

Метод `GET`, URL `https://pay.sushi-house-39.ru/leadteh-demo/messages-7.json`, таймаут 5 секунд.

```javascript
// Отключаем обычный выход «Продолжить».
// Следующий блок будет выбран вручную через goToBlock().
disableContinue();

// LEADTEX уже разобрал JSON-ответ сервера и положил его в response.data.
var data = response.data;

// Проверяем одновременно HTTP-код и обязательные поля нашего API.
// Без массива items запускать отправку нельзя.
if (Number(response.status) !== 200 || !data || data.success !== true || !data.items) {
  // Сохраняем причину остановки в карточке контакта для диагностики.
  setContactVariable("demo_status", "fetch_error");
  setContactVariable("demo_http_status", response.status);

  // sendMessage() здесь используется один раз только для сообщения об ошибке.
  sendMessage("Не удалось получить тестовый массив. HTTP: " + response.status);
} else if (data.items.length === 0) {
  // Пустой массив является корректным ответом, поэтому обрабатываем его отдельно.
  setContactVariable("demo_status", "empty");
  sendMessage("Сервер вернул пустой массив.");
} else {
  // Берём первый элемент. Остальные элементы будет выбирать блок отправки.
  var item = data.items[0];

  // Контактные переменные сохраняются между повторными запусками HTTP-блока.
  // Массив превращаем в строку, потому что переменная LEADTEX хранит текст.
  setContactVariable("demo_items", JSON.stringify(data.items));
  setContactVariable("demo_total", data.items.length);

  // Индекс 0 означает, что следующим будет отправлен первый элемент массива.
  setContactVariable("demo_index", 0);
  setContactVariable("demo_status", "sending");

  // JSON.stringify экранирует кавычки и переносы строк внутри текста.
  // Полученное значение можно безопасно вставить в JSON-тело следующего запроса.
  setContactVariable("demo_text_json", JSON.stringify(item.text));

  // Формируем Telegram inline_keyboard для текущего элемента массива.
  setContactVariable("demo_markup_json", JSON.stringify({inline_keyboard:[[{text:item.button_text,url:item.url}]]}));

  // Передаём управление единственному блоку отправки.
  goToBlock(49905288);
}
```

### Блок 02: отправить и повторить

Метод `POST`, URL `https://api.telegram.org/bot{{API}}/sendMessage`, формат тела `json`.

```json
{"chat_id":"{{telegram_id}}","text":{{$demo_text_json}},"reply_markup":{{$demo_markup_json}},"disable_notification":true}
```

- `chat_id` – Telegram ID текущего контакта LEADTEX.
- `text` – заранее экранированный текст текущего элемента массива.
- `reply_markup` – готовая JSON-клавиатура, поэтому вокруг переменной нет кавычек.
- `disable_notification` – тестовые сообщения приходят без звукового уведомления.

```javascript
// Отключаем обычный выход «Продолжить».
// Повтор и завершение контролируются только кодом ниже.
disableContinue();

// Telegram Bot API возвращает разобранный JSON в response.data.
var data = response.data;

// Для успешной отправки нужны HTTP 200 и Telegram-поле ok=true.
// Один HTTP 200 не считается достаточной проверкой ответа API.
if (Number(response.status) !== 200 || !data || data.ok !== true) {
  // Фиксируем ошибку и HTTP-код в переменных контакта.
  setContactVariable("demo_status", "send_error");
  setContactVariable("demo_http_status", response.status);

  // При Telegram 429 поле retry_after показывает паузу перед повтором.
  setContactVariable("demo_retry_after", data && data.parameters ? data.parameters.retry_after || 0 : 0);

  // Сообщаем, на каком элементе остановилась серия.
  sendMessage("Тест остановлен. Отправлено: " + getContactVariable("demo_index") + ". Проверь demo_http_status и demo_retry_after.");
} else {
  // Восстанавливаем исходный массив из строковой переменной LEADTEX.
  var items = JSON.parse(getContactVariable("demo_items"));

  // Предыдущее сообщение уже отправлено, поэтому увеличиваем индекс на единицу.
  var index = Number(getContactVariable("demo_index")) + 1;
  setContactVariable("demo_index", index);

  // Сохраняем ID последнего сообщения для диагностики или последующего удаления.
  setContactVariable("demo_last_message_id", data.result.message_id);

  // Продолжаем, пока в массиве есть элементы.
  // Ограничение index < 25 защищает сценарий от бесконечного или слишком большого цикла.
  if (index < items.length && index < 25) {
    // Подготавливаем текст и кнопку следующего элемента.
    var item = items[index];
    setContactVariable("demo_text_json", JSON.stringify(item.text));
    setContactVariable("demo_markup_json", JSON.stringify({inline_keyboard:[[{text:item.button_text,url:item.url}]]}));

    // Запускаем этот же HTTP-блок ещё раз с новыми переменными.
    goToBlock(49905288);
  } else {
    // Все элементы отправлены либо достигнут защитный предел.
    setContactVariable("demo_status", "complete");
  }
}
```

## Nginx

```nginx
# Все запросы с этим префиксом читаются из отдельного каталога демо.
location ^~ /leadteh-demo/ {
    # alias заменяет URL-префикс на путь /var/www/leadteh-demo/.
    alias /var/www/leadteh-demo/;

    # Не показываем список файлов при запросе каталога.
    autoindex off;

    # Файлы в этом location отдаются как JSON в UTF-8.
    default_type application/json;
    charset utf-8;

    # Не кешируем тестовые ответы, чтобы изменения были видны сразу.
    add_header Cache-Control "no-store";
    add_header X-Content-Type-Options nosniff;

    # Разрешаем только чтение. POST, PUT и DELETE получат отказ.
    limit_except GET { deny all; }
}
```

## Проверка

1. Открыть прямую тестовую ссылку.
2. Нажать `START` в Telegram, если бот ещё не был запущен этим пользователем.
3. Получить семь отдельных сообщений и семь inline-кнопок.
4. Проверить переменные контакта: `demo_status=complete`, `demo_index=7`, `demo_total=7`.

В живом тесте 10 сентября 2026 года отправлены все семь сообщений. Проверка на 25 элементах подготовлена, но не запускалась, чтобы не отправлять лишние сообщения в рабочий чат.
