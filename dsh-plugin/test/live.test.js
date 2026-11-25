import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import test from 'node:test'

// Real `defineTool` plus a real CLI subprocess. Skipped when the DSH packages
// are not installed next to this plugin (a standalone checkout).
let apply = null
try {
  ;({ apply } = await import('../index.js'))
} catch {
  apply = null
}

const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..')
const skip = apply === null
  ? 'install @deepseek-ai/dsh-tools (or run inside a DSH checkout) to run this test'
  : false

function realSpawn(spec) {
  const child = spawn(spec.argv[0], spec.argv.slice(1), {
    cwd: spec.cwd,
    env: { ...process.env, ...spec.env },
    stdio: ['ignore', 'pipe', 'pipe'],
  })
  let stdout = ''
  let stderr = ''
  child.stdout.setEncoding('utf8')
  child.stderr.setEncoding('utf8')
  child.stdout.on('data', (chunk) => { stdout += chunk })
  child.stderr.on('data', (chunk) => { stderr += chunk })
  const done = new Promise((resolve, reject) => {
    child.on('error', reject)
    child.on('close', (exitCode, signal) => resolve({ exitCode, signal }))
  })
  spec.signal?.addEventListener('abort', () => child.kill('SIGTERM'), { once: true })
  return {
    done,
    collected: {
      stdout: { readFrom: () => ({ text: stdout, nextOffset: stdout.length, lossy: false }) },
      stderr: { readFrom: () => ({ text: stderr, nextOffset: stderr.length, lossy: false }) },
    },
  }
}

test('plugin drives the real CLI through put, list, and restore', { skip }, async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'dsh-trash-live-'))
  const workspace = path.join(root, 'ws')
  const bin = path.join(root, 'bin')
  fs.mkdirSync(workspace)
  fs.mkdirSync(bin)
  const cli = path.join(bin, 'dsh-trash')
  fs.writeFileSync(
    cli,
    `#!/bin/sh\nPYTHONPATH='${repoRoot.replaceAll("'", "'\\''")}/src' exec python3 -m dsh_trash "$@"\n`,
  )
  fs.chmodSync(cli, 0o755)

  const previousDataHome = process.env.XDG_DATA_HOME
  process.env.XDG_DATA_HOME = path.join(root, 'data')
  try {
    const tools = new Map()
    const state = { mode: 'workspace-write' }
    const ctx = {
      tools: { register: (definition) => tools.set(definition.name, definition) },
      sandboxPolicy: { resolve: () => ({ mode: state.mode, workspaceRoot: workspace }) },
      workingDirectory: { ensure: async () => workspace },
      subprocess: { spawn: realSpawn },
    }
    apply(ctx, { cliPath: cli, timeoutMs: 10_000, preview: true })
    const exec = { agent: { session: { id: 'live' } }, signal: new AbortController().signal }

    const source = path.join(workspace, 'a.txt')
    fs.writeFileSync(source, 'hello from live test\n')
    const put = await tools.get('trash').execute({ path: 'a.txt', reason: 'live' }, exec)
    assert.equal(fs.existsSync(source), false)
    assert.ok(fs.existsSync(path.join(process.env.XDG_DATA_HOME, 'Trash', 'files', 'a.txt')))
    assert.ok(fs.existsSync(path.join(process.env.XDG_DATA_HOME, 'Trash', 'info', 'a.txt.trashinfo')))

    const listed = await tools.get('trash_list').execute({ limit: 10 }, exec)
    assert.equal(listed.total, 1)
    assert.equal(listed.entries[0].id, put.id)

    const restored = await tools.get('trash_restore').execute({ id: put.id }, exec)
    assert.equal(restored.relpath, 'a.txt')
    assert.equal(fs.readFileSync(source, 'utf8'), 'hello from live test\n')
    assert.equal(
      fs.existsSync(path.join(process.env.XDG_DATA_HOME, 'Trash', 'info', 'a.txt.trashinfo')),
      false,
    )

    state.mode = 'read-only'
    await assert.rejects(
      () => tools.get('trash').execute({ path: 'a.txt' }, exec),
      /read-only mode/,
    )
    state.mode = 'workspace-write'
    await assert.rejects(
      () => tools.get('trash').execute({ path: '../escape.txt' }, exec),
      /outside the session workspace/,
    )
  } finally {
    if (previousDataHome === undefined) delete process.env.XDG_DATA_HOME
    else process.env.XDG_DATA_HOME = previousDataHome
    fs.rmSync(root, { recursive: true, force: true })
  }
})
