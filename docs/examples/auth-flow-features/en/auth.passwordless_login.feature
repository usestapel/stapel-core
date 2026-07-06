# Generated from flow auth.passwordless_login by generate_flow_features — do not edit; regenerate (flow-system.md §3).
@flow:auth.passwordless_login
Feature: Passwordless login (email OTP)

  An anonymous user receives a one-time code by email and exchanges it for a JWT session (cookies + a token pair in the response body). Requesting the code again is rate-limited (30 seconds between sends; 429/422 when exceeded); after a series of wrong codes the address is temporarily locked. If the address was not registered, the first successful login creates a new user (status=REGISTERED instead of LOGGED_IN).

  # Actors: Anonymous user
  Scenario: Passwordless login (email OTP)
    Given The user enters their email on the login form
    When Request a one-time code by email; 429 on rate limit, 422 when the address is locked
    And Exchange the code for a JWT session; a wrong code decrements the attempt counter
    Then Emitted on first login — the profile and workspace are created by subscribers
