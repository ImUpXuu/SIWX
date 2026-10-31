'use strict';
// Validate generated JScript control flow with mock Windows Script Host objects.
// This does not run CMD or prove Task Scheduler behavior on Windows.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(process.argv[2], 'utf8');
new vm.Script(source);
for (const scenario of [
  { name: 'success', nativeCode: 0, hasResult: true, expected: 0 },
  { name: 'native failure', nativeCode: 7, hasResult: true, expected: 7 },
  { name: 'missing result', nativeCode: 0, hasResult: false, expected: 1 },
  { name: 'host error', throwError: true, hasResult: false, expected: 1 },
]) {
  const logs = [];
  const exits = [];
  const files = new Map();
  let runCount = 0;
  const fileSystem = {
    OpenTextFile() { return { WriteLine: s => logs.push(s), Close() {} }; },
    FileExists(path) { return scenario.hasResult || files.has(path); },
    CreateTextFile(path) {
      return { WriteLine: s => files.set(path, s), Close() {} };
    },
    MoveFile(from, to) {
      assert.equal(files.get(from), '1');
      files.set(to, files.get(from));
      files.delete(from);
    },
  };
  const shell = {
    Run(command, style, wait) {
      runCount++;
      assert.equal(style, 0, 'launcher must request a hidden window');
      assert.equal(wait, true, 'launcher must wait for CMD');
      assert.match(command, /cmd\.exe" \/d \/s \/c "".*\.cmd""$/);
      if (scenario.throwError) throw { number: 5, description: 'test launch denied' };
      return scenario.nativeCode;
    },
  };
  function ActiveXObject(name) {
    if (name === 'Scripting.FileSystemObject') return fileSystem;
    if (name === 'WScript.Shell') return shell;
    throw new Error('Unexpected COM class');
  }
  vm.runInNewContext(source, { ActiveXObject, WScript: { Quit: n => exits.push(n) } });
  assert.equal(runCount, 1);
  assert.deepEqual(exits, [scenario.expected], scenario.name);
  if (!scenario.hasResult) assert(logs.some(s => s.startsWith('ERROR:')));
  console.log('PASS: hidden launcher ' + scenario.name);
}
