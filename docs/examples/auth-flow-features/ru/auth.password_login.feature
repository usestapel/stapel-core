# language: ru
# Generated from flow auth.password_login by generate_flow_features — do not edit; regenerate (flow-system.md §3).
@flow:auth.password_login
Функция: Вход по паролю (+ опциональный TOTP)

  Пользователь входит по логину (email/username) и паролю. Эндпоинт включается настройкой AUTH_PASSWORD_LOGIN. Неудачные попытки ведут к прогрессивной блокировке (423 c retry_after). Если у пользователя включён TOTP и настройка PASSWORD_LOGIN_STEP_UP активна (по умолчанию да), вместо токенов возвращается TOTP_REQUIRED c challenge_token — сессия выдаётся только после проверки кода аутентификатора.

  # Акторы: Anonymous user
  Сценарий: Вход по паролю (+ опциональный TOTP)
    Дано Пользователь вводит логин и пароль на форме входа
    Когда Проверить пароль; 423 при блокировке; при включённом TOTP и PASSWORD_LOGIN_STEP_UP — ответ TOTP_REQUIRED c challenge_token
    Тогда Опциональный шаг (только при TOTP_REQUIRED): обменять challenge_token и код аутентификатора на JWT-сессию
