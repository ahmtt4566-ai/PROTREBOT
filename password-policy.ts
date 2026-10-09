import policy from './backend/app/password_policy.json'

export const PASSWORD_MIN_LENGTH = policy.min_length
export const PASSWORD_MAX_LENGTH = policy.max_length
const patterns = {
  uppercase: new RegExp(policy.patterns.uppercase),
  lowercase: new RegExp(policy.patterns.lowercase),
  digit: new RegExp(policy.patterns.digit),
  symbol: new RegExp(policy.patterns.symbol),
}

export function passwordRules(password: string) {
  const length = Array.from(password).length
  return [
    [`${PASSWORD_MIN_LENGTH}–${PASSWORD_MAX_LENGTH} karakter`, length >= PASSWORD_MIN_LENGTH && length <= PASSWORD_MAX_LENGTH],
    ['Büyük harf', patterns.uppercase.test(password)],
    ['Küçük harf', patterns.lowercase.test(password)],
    ['Rakam', patterns.digit.test(password)],
    ['Sembol', patterns.symbol.test(password)],
  ] as const
}

export function passwordPolicyError(password: string): string | null {
  const missing = passwordRules(password).filter(([, passed]) => !passed).map(([label]) => label)
  return missing.length ? `Eksik parola şartları: ${missing.join(', ')}.` : null
}
