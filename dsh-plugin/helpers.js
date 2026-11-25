/**
 * Pure helpers for the trash plugin: path policy, argv construction, and the
 * scrubbed environment the CLI needs to find the user's trash directory.
 * Kept free of DSH imports so it can be unit-tested with plain `node --test`.
 */

import path from 'node:path'

/** True when `target` is strictly below `workspace`. */
export function isInside(workspace, target) {
  const rel = path.relative(workspace, target)
  return rel !== '' && rel !== '..' && !rel.startsWith(`..${path.sep}`) && !path.isAbsolute(rel)
}

/**
 * Resolve a model-supplied path and enforce the workspace boundary lexically.
 * The CLI performs the canonical, symlink-aware check immediately before the
 * mutation; this is the early, cheap refusal.
 */
export function resolveTargetPath(input, cwd, workspace) {
  const target = path.resolve(cwd, input)
  if (!isInside(workspace, target)) {
    throw new Error(`path is outside the session workspace: ${target}`)
  }
  if (path.basename(target) === '.git') {
    throw new Error('refusing to trash protected path: .git')
  }
  return target
}

/** Build the argv for `put`, never through a shell. */
export function putArgs(target, workspace, { reason, preview = true } = {}) {
  const args = ['put', target, '--workspace', workspace, '--json']
  if (reason) args.push('--reason', reason)
  if (!preview) args.push('--no-preview')
  return args
}

/** Build the argv for `list`. */
export function listArgs(workspace, limit) {
  return ['list', '--workspace', workspace, '--limit', String(limit), '--json']
}

/** Build the argv for `restore`. */
export function restoreArgs(entryId, workspace, force = false) {
  const args = ['restore', entryId, '--workspace', workspace, '--json']
  if (force) args.push('--force')
  return args
}

/** Only pass the variables the CLI needs; DSH hands children a scrubbed env. */
export function cliEnv(environment = process.env) {
  const env = {}
  for (const key of ['HOME', 'USERPROFILE', 'XDG_DATA_HOME', 'PATH']) {
    const value = environment[key]
    if (value) env[key] = value
  }
  return env
}

/** Extract collected stdout/stderr text from a finished subprocess handle. */
export function collectedText(handle) {
  return {
    stdout: handle.collected.stdout?.readFrom(0).text ?? '',
    stderr: handle.collected.stderr?.readFrom(0).text ?? '',
  }
}

/** Turn a CLI error envelope into a model-facing Error. */
export function cliFailure(exitCode, stdout, stderr) {
  try {
    const parsed = JSON.parse(stdout)
    if (parsed?.error?.message) {
      return new Error(`${parsed.error.code ?? 'TRASH_ERROR'}: ${parsed.error.message}`)
    }
  } catch {
    // fall through to stderr / exit code
  }
  const detail = stderr.trim() || stdout.trim() || `exit code ${exitCode}`
  return new Error(`dsh-trash failed: ${detail}`)
}
