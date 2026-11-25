import assert from 'node:assert/strict'
import test from 'node:test'

// The real tool factory comes from the DSH installation. A clean standalone
// checkout does not have it, so this suite skips instead of failing.
let apply = null
try {
  ;({ apply } = await import('../index.js'))
} catch {
  apply = null
}

const skip = apply === null
  ? 'install @deepseek-ai/dsh-tools (or run inside a DSH checkout) to run integration tests'
  : false

function reader(value) {
  return { readFrom: () => ({ text: JSON.stringify(value), nextOffset: 0, lossy: false }) }
}

function emptyReader() {
  return { readFrom: () => ({ text: '', nextOffset: 0, lossy: false }) }
}

function fakeContext({ mode = 'workspace-write', workspace = '/ws' } = {}) {
  const tools = new Map()
  const spawns = []
  const state = { mode, workspace }
  const ctx = {
    tools: { register: (definition) => tools.set(definition.name, definition) },
    sandboxPolicy: { resolve: () => ({ mode: state.mode, workspaceRoot: state.workspace }) },
    workingDirectory: { ensure: async () => state.workspace },
    subprocess: {
      spawn(spec) {
        spawns.push(spec)
        const payload = spec.argv[1] === 'put'
          ? {
              id: 't_1', workspace, path: `${workspace}/a.txt`, relpath: 'a.txt',
              kind: 'file', size: 3, sha256: 'abc', trashed_at: '2026-01-01T00:00:00.000Z',
            }
          : spec.argv[1] === 'list'
            ? {
                entries: [{
                  id: 't_1', relpath: 'a.txt', kind: 'file', size: 3,
                  trashed_at: '2026-01-01T00:00:00.000Z', state: 'active',
                }],
                total: 1,
              }
            : {
                id: 't_1', workspace, path: `${workspace}/a.txt`, relpath: 'a.txt',
                sha256: 'abc', restored_at: '2026-01-01T00:00:01.000Z',
              }
        return {
          done: Promise.resolve({ exitCode: 0, signal: null }),
          collected: { stdout: reader(payload), stderr: emptyReader() },
        }
      },
    },
  }
  apply(ctx, { cliPath: 'dsh-trash', timeoutMs: 5_000, preview: true })
  return { ctx, tools, spawns, state }
}

const exec = { agent: { session: { id: 's' } }, signal: new AbortController().signal }

test('registers exactly three tools and drives the CLI with absolute argv', { skip }, async () => {
  const { tools, spawns } = fakeContext()
  assert.deepEqual([...tools.keys()].sort(), ['trash', 'trash_list', 'trash_restore'])

  const put = await tools.get('trash').execute({ path: 'a.txt', reason: 'cleanup' }, exec)
  assert.equal(put.id, 't_1')
  assert.deepEqual(spawns[0].argv, [
    'dsh-trash', 'put', '/ws/a.txt', '--workspace', '/ws', '--json', '--reason', 'cleanup',
  ])
  assert.equal(spawns[0].env.HOME, process.env.HOME)

  const list = await tools.get('trash_list').execute({ limit: 5 }, exec)
  assert.equal(list.total, 1)
  assert.deepEqual(spawns[1].argv, [
    'dsh-trash', 'list', '--workspace', '/ws', '--limit', '5', '--json',
  ])

  const restored = await tools.get('trash_restore').execute({ id: 't_1', force: true }, exec)
  assert.equal(restored.relpath, 'a.txt')
  assert.deepEqual(spawns[2].argv, [
    'dsh-trash', 'restore', 't_1', '--workspace', '/ws', '--json', '--force',
  ])
})

test('refuses read-only mode and paths outside the workspace', { skip }, async () => {
  const { tools, spawns, state } = fakeContext()
  state.mode = 'read-only'
  await assert.rejects(
    () => tools.get('trash').execute({ path: 'a.txt' }, exec),
    /read-only mode/,
  )
  assert.equal(spawns.length, 0)

  state.mode = 'workspace-write'
  await assert.rejects(
    () => tools.get('trash').execute({ path: '../escape.txt' }, exec),
    /outside the session workspace/,
  )
  assert.equal(spawns.length, 0)
})
