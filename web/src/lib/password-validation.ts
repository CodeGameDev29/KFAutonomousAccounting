/**
 * Password complexity validation.
 *
 * The server enforces these same rules in _validate_password_strength. They
 * are duplicated here so the form can answer instantly before calling signUp()
 * or updateUser() — for feedback, never as the thing that enforces the rule.
 *
 * Every sentence the UI shows about the rule is derived from this module —
 * `NEW_PASSWORD_MIN_LENGTH` and `NEW_PASSWORD_HINT` below. A placeholder typed
 * out by hand in a form drifts from the rule the moment the rule changes, and
 * a field that says one thing and rejects another is worse than a blank one.
 */

/** The shortest password signup / reset will accept. */
export const NEW_PASSWORD_MIN_LENGTH = 8;

export interface PasswordCheck {
  label: string;
  met: boolean;
}

export function getPasswordChecks(password: string): PasswordCheck[] {
  return [
    {
      label: `At least ${NEW_PASSWORD_MIN_LENGTH} characters`,
      met: password.length >= NEW_PASSWORD_MIN_LENGTH,
    },
    { label: "One uppercase letter", met: /[A-Z]/.test(password) },
    { label: "One lowercase letter", met: /[a-z]/.test(password) },
    { label: "One number", met: /\d/.test(password) },
    { label: "One special character (!@#$...)", met: /[^A-Za-z0-9]/.test(password) },
  ];
}

/**
 * One line describing the enforced rule, for a field placeholder or hint.
 * Built from the same checks `validateNewPassword` applies, so the two can
 * never disagree: "8+ chars, upper, lower, number, symbol".
 */
export const NEW_PASSWORD_HINT = `${NEW_PASSWORD_MIN_LENGTH}+ chars, upper, lower, number, symbol`;

/** Number of checks that pass (0-5). */
export function getPasswordScore(password: string): number {
  return getPasswordChecks(password).filter((c) => c.met).length;
}

/** Human-readable strength label. */
export function getPasswordStrengthLabel(password: string): string {
  if (!password) return "";
  const score = getPasswordScore(password);
  if (score <= 2) return "Weak";
  if (score <= 3) return "Fair";
  if (score <= 4) return "Good";
  return "Strong";
}

/** Color class for the strength label. */
export function getPasswordStrengthColor(password: string): string {
  const score = getPasswordScore(password);
  if (score <= 2) return "text-destructive";
  if (score <= 3) return "text-amber-600";
  if (score <= 4) return "text-success";
  return "text-success";
}

/**
 * Validate a password for signup / reset. Returns an error string or undefined.
 * For sign-in, use validateSignInPassword() which is lenient (existing users).
 */
export function validateNewPassword(password: string): string | undefined {
  if (!password) return "Password is required.";
  if (password.length < NEW_PASSWORD_MIN_LENGTH)
    return `Password must be at least ${NEW_PASSWORD_MIN_LENGTH} characters.`;
  if (!/[A-Z]/.test(password)) return "Password needs at least one uppercase letter.";
  if (!/[a-z]/.test(password)) return "Password needs at least one lowercase letter.";
  if (!/\d/.test(password)) return "Password needs at least one number.";
  if (!/[^A-Za-z0-9]/.test(password)) return "Password needs at least one special character.";
  return undefined;
}

/**
 * Sign-in only checks that something plausible was typed. An account may hold a
 * password that predates the current rule, and rejecting it here would lock out
 * a user whose credentials the server still accepts.
 */
export function validateSignInPassword(password: string): string | undefined {
  if (!password) return "Password is required.";
  if (password.length < 6) return "Password must be at least 6 characters.";
  return undefined;
}
