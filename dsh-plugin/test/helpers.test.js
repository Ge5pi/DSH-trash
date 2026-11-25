import assert from 'node:assert/strict'
import test from 'node:test'

import {
  cliEnv,
  cliFailure,
  isInside,
  listArgs,
  putArgs,
  resolveTargetPath,
  restoreArgs,
} from '../helpers.js'

const workspace = '/home/u/proj'

test('isInside accepts descendants and rejects the root and siblings', () => {
  assert.equal(isInside(workspace, '/home/u/proj/a.txt'), true)
  assert.equal(isInside(workspace, '/home/u/proj/sub/a.txt'), true)
  assert.equal(isInside(workspace, workspace), false)
  assert.equal(isInside(workspace, '/home/u/proj2/a.txt'), false)
  assert.equal(isInside(workspace, '/home/u/other/a.txt'), false)
})

test('resolveTargetPath resolves relative paths and rejects escapes', () => {
  assert.equal(resolveTargetPath('sub/a.txt', workspace, workspace), '/home/u/proj/sub/a.txt')
  assert.throws(() => resolveTargetPath('../outside.txt', workspace, workspace), /outside the session workspace/)
  assert.throws(() => resolveTargetPath('/etc/passwd', workspace, workspace), /outside the session workspace/)
  assert.throws(() => resolveTargetPath('.git', workspace, workspace), /protected path/)
})

test('putArgs never adds a shell and carries the workspace', () => {
  assert.deepEqual(putArgs('/home/u/proj/a.txt', workspace), [
    'put', '/home/u/proj/a.txt', '--workspace', workspace, '--json',
  ])
  assert.deepEqual(putArgs('/home/u/proj/a.txt', workspace, { reason: 'cleanup', preview: false }), [
    'put', '/home/u/proj/a.txt', '--workspace', workspace, '--json', '--reason', 'cleanup', '--no-preview',
  ])
})

test('listArgs and restoreArgs', () => {
  assert.deepEqual(listArgs(workspace, 20), ['list', '--workspace', workspace, '--limit', '20', '--json'])
  assert.deepEqual(restoreArgs('t_1', workspace), ['restore', 't_1', '--workspace', workspace, '--json'])
  assert.deepEqual(restoreArgs('t_1', workspace, true), [
    'restore', 't_1', '--workspace', workspace, '--json', '--force',
  ])
})

test('cliEnv keeps only the trash-relevant variables', () => {
  assert.deepEqual(
    cliEnv({ HOME: '/home/u', PATH: '/bin', SECRET: 'x', XDG_DATA_HOME: '/data' }),
    { HOME: '/home/u', XDG_DATA_HOME: '/data', PATH: '/bin' },
  )
})

test('cliFailure prefers the JSON envelope and falls back to stderr', () => {
  const envelope = cliFailure(5, JSON.stringify({ error: { code: 'OUTSIDE_WORKSPACE', message: 'no' } }), '')
  assert.match(envelope.message, /OUTSIDE_WORKSPACE: no/)
  const fallback = cliFailure(6, 'not json', 'disk full')
  assert.match(fallback.message, /disk full/)
})
