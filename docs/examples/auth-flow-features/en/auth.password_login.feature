# Generated from flow auth.password_login by generate_flow_features — do not edit; regenerate (flow-system.md §3).
@flow:auth.password_login
Feature: Password login (+ optional TOTP)

  The user signs in with a login (email/username) and password. The endpoint is enabled by the AUTH_PASSWORD_LOGIN setting. Failed attempts lead to progressive lockout (423 with retry_after). If the user has TOTP enabled and the PASSWORD_LOGIN_STEP_UP setting is active (default: yes), TOTP_REQUIRED with a challenge_token is returned instead of tokens — the session is issued only after the authenticator code is verified.

  # Actors: Anonymous user
  Scenario: Password login (+ optional TOTP)
    Given The user enters their login and password on the login form
    When Verify the password; 423 when locked out; with TOTP enabled and PASSWORD_LOGIN_STEP_UP — a TOTP_REQUIRED response with a challenge_token
    Then Optional step (only on TOTP_REQUIRED): exchange the challenge_token and the authenticator code for a JWT session
