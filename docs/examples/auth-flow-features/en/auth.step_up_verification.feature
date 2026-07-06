# Generated from flow auth.step_up_verification by generate_flow_features — do not edit; regenerate (flow-system.md §3).
@flow:auth.step_up_verification
Feature: Step-up verification on a protected endpoint (reference flow)

  THE reference flow of the step-up verification contract (stapel_core.verification, see flows-and-verification.md §2) — clients of any service implement it once and reuse it for every endpoint protected by @requires_verification. The cycle: the protected endpoint responds 403 with a structured verification envelope (challenge_id, scope, factors, expires_at) → the client reads the challenge, picks an available factor (factors are interchangeable: otp_email, otp_phone, totp, passkey all close one challenge), initiates it and completes the check → repeats the original request. The grant is stored server-side (cache, user+scope key, TTL=max_age); stateless clients may instead send the X-Verification-Token header from the completion response. After MAX_ATTEMPTS wrong attempts the challenge burns out (423) — call the original endpoint again for a new challenge.

  # Actors: Authenticated user
  Scenario: Step-up verification on a protected endpoint (reference flow)
    Given The client calls the protected endpoint and receives 403 with a verification envelope: challenge_id, scope, factors, expires_at
    When Read the challenge: the scope and the factors filtered down to those actually available to the user; 404 for a foreign/expired challenge
    And Initiate the chosen factor: send a code (otp_email/otp_phone) or get WebAuthn options (passkey); totp needs no initiation
    And Complete the challenge with the factor proof; success = {verified, verification_token} + a server-side grant; 400 on a wrong code, 423 when the challenge burned out from brute force
    And Repeat the original request — the grant is already on the server; a stateless client sends the X-Verification-Token from the completion response
    And Optional: view your step-up preferences — one {scope, enabled} row per scope the user has touched (enabled=false disables a default_on scope, enabled=true enables an opt_in scope; strict endpoints ignore the preferences)
    Then Optional: change a {scope, enabled} preference. INVARIANT: disabling (enabled=false) is itself protected by @requires_verification(scope=verification.settings, level=default_on) — without a fresh grant a 403 with a verification envelope is returned; enabling requires no step-up confirmation. Both writes reset the policy cache in core
