import assert from 'node:assert/strict'
import {test} from 'node:test'
import {
  CHAT_MAX_AGE_MS, CHAT_MAX_MESSAGE_CHARS, chatStorageKey, createKaisChatStorage, maskChatSecrets, parseChatRecord,
  clearKaisChatHistory, resumeKaisChatHistory,
} from '../frontend/src/kais-chat-storage.ts'

const now = Date.UTC(2026, 9, 3, 12)
const row = (id, content = `Mesaj ${id}`) => ({id: String(id), role: 'user', language: 'tr', content})
const record = (messages, savedAt = now) => JSON.stringify({version: 1, savedAt, messages})
function memoryStorage() {
  const values = new Map()
  return {
    get length() { return values.size },
    key(index) { return [...values.keys()][index] ?? null },
    getItem(key) { return values.get(key) ?? null },
    setItem(key, value) { values.set(key, value) },
    removeItem(key) { values.delete(key) },
    clear() { values.clear() },
  }
}

for (const value of [
  'Bearer a-short-private-value', 'Basic dGVzdDp0ZXN0', 'sk-demo-private-value', 'sk-ant-demo-private-value',
  'api_key=smallValue', 'secret:smallValue', 'token=smallValue', 'key=smallValue',
  'password="two private words"', 'parola:smallValue', '{"apiKey":"smallValue"}',
  'confirmation_token=signed-approval', 'consent_token=signed-approval', 'arm_token=signed-approval',
  'signed_approval=signed-approval', 'api-privateValue', 'secret-privateValue', 'token-privateValue',
  'token privateValue', 'secret key privateValue', 'api key privateValue',
  'key-privateValue', 'apiPrivateValue', 'aB3d'.repeat(12),
  'abcdefgh123.abcdefgh456.abcdefgh789',
]) {
  test(`Masks credential-like text: ${value.split(/[=: ]/)[0]}`, () => {
    const masked = maskChatSecrets(value)
    assert.notEqual(masked, value)
    assert.ok(masked.includes('[MASKED]'))
    assert.equal(maskChatSecrets(masked), masked)
  })
}

test('Normal platform text is preserved, exact known approval proofs are removed everywhere', () => {
  const normal = 'Plan, kredi ve API bağlantısı hakkında yardım istiyorum.'
  assert.equal(maskChatSecrets(normal), normal)
  assert.equal(maskChatSecrets('Proof short-proof and short-proof.', ['short-proof']), 'Proof [MASKED] and [MASKED].')
})

test('Writes only 50 completed messages, strips metadata and caps Unicode content after redaction', () => {
  const storage = memoryStorage()
  const chat = createKaisChatStorage('member', () => storage, () => now)
  const messages = Array.from({length: 55}, (_, i) => row(i))
  messages.push({...row('pending'), pending: true}, {...row('failed'), failed: true}, row('empty', ' '))
  messages[54] = {...row(54, '🙂'.repeat(4001)), confirmation_token: 'private-proof', authorization: 'private-auth'}
  chat.write(messages)
  const saved = JSON.parse(storage.getItem(chatStorageKey('member')))
  assert.equal(saved.messages.length, 50)
  assert.equal(saved.messages[0].id, '5')
  assert.equal(Array.from(saved.messages.at(-1).content).length, CHAT_MAX_MESSAGE_CHARS)
  assert.deepEqual(Object.keys(saved).sort(), ['messages', 'savedAt', 'version'])
  assert.equal(JSON.stringify(saved).includes('private-proof'), false)
  assert.equal(JSON.stringify(saved).includes('private-auth'), false)
  assert.deepEqual(chat.read(), saved.messages)
})

test('Version, JSON shape, duplicate IDs, empty messages, unknown fields and expired records are rejected', () => {
  for (const raw of ['{', 'null', '[]', JSON.stringify({version: 0, savedAt: now, messages: []}),
    record([row('same'), row('same')]), record([row(1, '')]), record([{...row(1), confirmation_token: 'proof'}]),
    record([row(1)], now - CHAT_MAX_AGE_MS - 1), record([row(1)], now + 1)]) {
    assert.equal(parseChatRecord(raw, now), null)
    const storage = memoryStorage()
    storage.setItem(chatStorageKey('member'), raw)
    assert.deepEqual(createKaisChatStorage('member', () => storage, () => now).read(), [])
    assert.equal(storage.getItem(chatStorageKey('member')), null)
  }
  assert.ok(parseChatRecord(record([row(1)], now - CHAT_MAX_AGE_MS), now))
})

test('Loading masks unsafe pre-existing text without renewing its age', () => {
  const storage = memoryStorage()
  const savedAt = now - 1000
  storage.setItem(chatStorageKey('member'), record([row(1, 'api-privateValue')], savedAt))
  const chat = createKaisChatStorage('member', () => storage, () => now)
  assert.equal(chat.read()[0].content, '[MASKED]')
  assert.equal(JSON.parse(storage.getItem(chatStorageKey('member'))).savedAt, savedAt)
})

test('New identity deletes all foreign chat versions but leaves unrelated storage alone', () => {
  const storage = memoryStorage()
  storage.setItem(chatStorageKey('old'), record([row(1)]))
  storage.setItem('kais-chat:v0:old', 'old-format')
  storage.setItem('unrelated', 'keep')
  storage.setItem(chatStorageKey('new'), record([row(2)]))
  const chat = createKaisChatStorage('new', () => storage, () => now)
  assert.equal(storage.getItem(chatStorageKey('old')), null)
  assert.equal(storage.getItem('kais-chat:v0:old'), null)
  assert.equal(storage.getItem('unrelated'), 'keep')
  assert.equal(chat.read()[0].id, '2')
  chat.clear()
  assert.equal(storage.getItem(chatStorageKey('new')), null)
})

test('Anonymous storage never accesses a browser and stopped persistence cannot resurrect a record', () => {
  const anonymous = createKaisChatStorage(null, () => { throw new Error('Must not access storage') })
  anonymous.write([row(1)])
  assert.deepEqual(anonymous.read(), [])
  const storage = memoryStorage()
  const chat = createKaisChatStorage('member', () => storage, () => now)
  chat.stop()
  chat.write([row(1)])
  assert.equal(storage.length, 0)
})

test('Denied access and quota failures warn once, disable persistence and never throw', t => {
  const warn = t.mock.method(console, 'warn', () => {})
  let accesses = 0
  const denied = createKaisChatStorage('member', () => { accesses++; throw new Error('Denied') })
  assert.deepEqual(denied.read(), [])
  denied.write([row(1)])
  denied.clear()
  assert.equal(accesses, 1)
  const storage = memoryStorage()
  const full = createKaisChatStorage('member', () => storage, () => now)
  storage.setItem = () => { throw new Error('Quota') }
  full.write([row(1)])
  full.write([row(2)])
  assert.deepEqual(full.read(), [])
  assert.equal(warn.mock.callCount(), 2)
})

test('Logout invalidates every old writer permanently, including after a verified login resumes', () => {
  const storage = memoryStorage()
  const oldWindow = globalThis.window
  globalThis.window = {localStorage: storage, dispatchEvent: () => true}
  try {
    const oldWriter = createKaisChatStorage('member', () => storage, () => now)
    oldWriter.write([row(1)])
    clearKaisChatHistory()
    oldWriter.write([row(2)])
    assert.equal(storage.length, 0)
    const pausedWriter = createKaisChatStorage('member', () => storage, () => now)
    pausedWriter.write([row(3)])
    assert.equal(storage.length, 0)
    resumeKaisChatHistory('member')
    oldWriter.write([row(4)])
    pausedWriter.write([row(4)])
    assert.equal(storage.length, 0)
    const newWriter = createKaisChatStorage('new-member', () => storage, () => now)
    newWriter.write([row(5)])
    assert.equal(JSON.parse(storage.getItem(chatStorageKey('new-member'))).messages[0].id, '5')
  } finally {
    resumeKaisChatHistory('member')
    if (oldWindow === undefined) delete globalThis.window
    else globalThis.window = oldWindow
  }
})
