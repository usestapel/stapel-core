# language: ru
# Generated from flow auth.step_up_verification by generate_flow_features — do not edit; regenerate (flow-system.md §3).
@flow:auth.step_up_verification
Функция: Step-up-верификация на защищённом эндпоинте (референсный флоу)

  РЕФЕРЕНСНЫЙ флоу контракта step-up-верификации (stapel_core.verification, см. flows-and-verification.md §2) — клиенты любого сервиса реализуют его один раз и переиспользуют для всех эндпоинтов, защищённых @requires_verification. Цикл: защищённый эндпоинт отвечает 403 со структурированным конвертом verification (challenge_id, scope, factors, expires_at) → клиент читает challenge, выбирает доступный фактор (факторы взаимозаменяемы: otp_email, otp_phone, totp, passkey закрывают один challenge), инициирует его и завершает проверку → повторяет исходный запрос. Grant хранится сервер-сайд (cache, ключ user+scope, TTL=max_age); stateless-клиенты могут вместо этого прислать заголовок X-Verification-Token из ответа завершения. После MAX_ATTEMPTS неверных попыток challenge сгорает (423) — нужно снова вызвать исходный эндпоинт за новым challenge.

  # Акторы: Authenticated user
  Сценарий: Step-up-верификация на защищённом эндпоинте (референсный флоу)
    Дано Клиент вызывает защищённый эндпоинт и получает 403 с конвертом verification: challenge_id, scope, factors, expires_at
    Когда Прочитать challenge: scope и факторы, отфильтрованные до реально доступных пользователю; 404 для чужого/истёкшего challenge
    И Инициировать выбранный фактор: отправить код (otp_email/otp_phone) или получить WebAuthn-опции (passkey); totp инициации не требует
    И Завершить challenge доказательством фактора; успех = {verified, verification_token} + grant сервер-сайд; 400 при неверном коде, 423 когда challenge сгорел от перебора
    И Повторить исходный запрос — grant уже на сервере; stateless-клиент передаёт X-Verification-Token из ответа завершения
    И Опционально: посмотреть свои step-up-настройки — по строке {scope, enabled} на каждый scope, который пользователь трогал (enabled=false выключает default_on-scope, enabled=true включает opt_in-scope; strict-эндпоинты настройки игнорируют)
    Тогда Опционально: изменить настройку {scope, enabled}. ИНВАРИАНТ: выключение (enabled=false) само защищено @requires_verification(scope=verification.settings, level=default_on) — без свежего grant'а придёт 403 с конвертом verification; включение step-up-подтверждения не требует. Обе записи сбрасывают кэш политики в core
