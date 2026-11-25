/**
 * DSH bundle for dsh-trash: three model-facing tools over the trusted host CLI.
 *
 * The plugin does not move files through `ctx.fs` (the file sandbox would deny
 * a write outside the workspace). It spawns the CLI as a trusted host child
 * and re-checks the session's sandbox mode and workspace boundary first.
 *
 * @module dsh-plugin-trash
 */

import { defineTool } from '@deepseek-ai/dsh-tools'
import {
  cliEnv,
  cliFailure,
  collectedText,
  listArgs,
  putArgs,
  resolveTargetPath,
  restoreArgs,
} from './helpers.js'

export const name = 'tool-trash'
export const inject = ['tools', 'sandboxPolicy', 'subprocess', 'workingDirectory']

const READ_ONLY =
  '[sandbox: file access denied under read-only mode] trash is unavailable in read-only mode'

function activePolicy(ctx, exec) {
  if (!exec.agent) throw new Error('trash requires an owning agent session')
  const policy = ctx.sandboxPolicy.resolve({ session: exec.agent.session })
  if (policy.mode === 'read-only') throw new Error(READ_ONLY)
  return policy
}

async function runCli(ctx, exec, config, argv) {
  const timeoutMs = config.timeoutMs ?? 20_000
  const signal = AbortSignal.any([exec.signal, AbortSignal.timeout(timeoutMs)])
  const cwd = await ctx.workingDirectory.ensure(exec.agent, exec.signal)
  const handle = ctx.subprocess.spawn({
    argv: [config.cliPath ?? 'dsh-trash', ...argv],
    cwd,
    stdio: {
      stdin: 'ignore',
      stdout: { maxBytes: 256 * 1024 },
      stderr: { maxBytes: 64 * 1024 },
    },
    graceMs: 1_000,
    signal,
    env: cliEnv(),
  })

  let outcome
  try {
    outcome = await handle.done
  } catch (error) {
    if (exec.signal.aborted) throw new Error('dsh-trash was aborted')
    throw error
  }
  if (exec.signal.aborted) throw new Error('dsh-trash was aborted')
  if (signal.aborted) throw new Error(`dsh-trash timed out after ${timeoutMs} ms`)

  const { stdout, stderr } = collectedText(handle)
  if (outcome.exitCode !== 0) throw cliFailure(outcome.exitCode, stdout, stderr)
  try {
    return JSON.parse(stdout)
  } catch {
    throw new Error('dsh-trash returned invalid JSON')
  }
}

function applyTrash(ctx, config) {
  ctx.tools.register(defineTool({
    name: 'trash',
    description:
      'Move a file or directory from the current workspace to a reversible trash. '
      + 'Prefer this over rm for deleting workspace content: the entry can be listed and restored. '
      + 'Only paths inside the session workspace are accepted.',
    parameters: {
      path: {
        type: 'string',
        required: true,
        description: 'Workspace-relative or absolute path inside the session workspace.',
      },
      reason: {
        type: 'string',
        description: 'Optional short reason, recorded in the trash metadata.',
      },
    },
    output: {
      schema: {
        type: 'object',
        additionalProperties: false,
        properties: {
          id: { type: 'string', required: true },
          path: { type: 'string', required: true },
          relpath: { type: 'string', required: true },
          kind: { type: 'string', required: true },
          size: { type: 'integer', required: true },
          sha256: { oneOf: [{ type: 'string' }, { type: 'null' }], required: true },
          trashed_at: { type: 'string', required: true },
        },
      },
      render: (_args, value) => [{
        type: 'text',
        text: `Trashed ${value.relpath} (${value.kind}, ${value.size} bytes) as ${value.id}.`,
      }],
    },
    async execute(args, exec) {
      const policy = activePolicy(ctx, exec)
      const cwd = await ctx.workingDirectory.ensure(exec.agent, exec.signal)
      const target = resolveTargetPath(args.path, cwd, policy.workspaceRoot)
      const result = await runCli(
        ctx,
        exec,
        config,
        putArgs(target, policy.workspaceRoot, { reason: args.reason, preview: config.preview !== false }),
      )
      return {
        id: result.id,
        path: result.path,
        relpath: result.relpath,
        kind: result.kind,
        size: result.size,
        sha256: result.sha256 ?? null,
        trashed_at: result.trashed_at,
      }
    },
  }))
}

function applyList(ctx, config) {
  ctx.tools.register(defineTool({
    name: 'trash_list',
    description:
      'List entries in the reversible trash that belong to the current workspace, newest first. '
      + 'Use an entry id with trash_restore to bring a file back.',
    parameters: {
      limit: {
        type: 'integer',
        description: 'Maximum entries to return (1-100, default 20).',
      },
    },
    output: {
      schema: {
        type: 'object',
        additionalProperties: false,
        properties: {
          entries: {
            type: 'array',
            required: true,
            items: {
              type: 'object',
              additionalProperties: false,
              properties: {
                id: { type: 'string', required: true },
                relpath: { type: 'string', required: true },
                kind: { type: 'string', required: true },
                size: { type: 'integer', required: true },
                trashed_at: { type: 'string', required: true },
                state: { type: 'string', required: true },
              },
            },
          },
          total: { type: 'integer', required: true },
        },
      },
      render: (_args, value) => [{
        type: 'text',
        text: value.entries.length === 0
          ? 'Trash is empty for this workspace.'
          : value.entries
            .map(entry => `${entry.id}  ${entry.relpath}  (${entry.kind}, ${entry.size} bytes, ${entry.trashed_at})`)
            .join('\n'),
      }],
    },
    async execute(args, exec) {
      const policy = activePolicy(ctx, exec)
      const limit = Math.min(100, Math.max(1, Number.isInteger(args.limit) ? args.limit : 20))
      const result = await runCli(ctx, exec, config, listArgs(policy.workspaceRoot, limit))
      return { entries: result.entries ?? [], total: result.total ?? 0 }
    },
  }))
}

function applyRestore(ctx, config) {
  ctx.tools.register(defineTool({
    name: 'trash_restore',
    description:
      'Restore a trash entry into the current workspace. The target is reconstructed from the '
      + 'entry relative path; if it already exists, pass force=true to move the existing file to '
      + 'the trash first. Restoring across workspaces is refused.',
    parameters: {
      id: {
        type: 'string',
        required: true,
        description: 'Entry id returned by trash or trash_list.',
      },
      force: {
        type: 'boolean',
        description: 'Replace an existing target by moving it to the trash (default false).',
      },
    },
    output: {
      schema: {
        type: 'object',
        additionalProperties: false,
        properties: {
          id: { type: 'string', required: true },
          path: { type: 'string', required: true },
          relpath: { type: 'string', required: true },
          sha256: { oneOf: [{ type: 'string' }, { type: 'null' }], required: true },
          restored_at: { type: 'string', required: true },
        },
      },
      render: (_args, value) => [{
        type: 'text',
        text: `Restored ${value.relpath} to ${value.path}.`,
      }],
    },
    async execute(args, exec) {
      const policy = activePolicy(ctx, exec)
      const result = await runCli(
        ctx,
        exec,
        config,
        restoreArgs(args.id, policy.workspaceRoot, args.force === true),
      )
      return {
        id: result.id,
        path: result.path,
        relpath: result.relpath,
        sha256: result.sha256 ?? null,
        restored_at: result.restored_at,
      }
    },
  }))
}

export function apply(ctx, config = {}) {
  applyTrash(ctx, config)
  applyList(ctx, config)
  applyRestore(ctx, config)
}
