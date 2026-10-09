const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const testModule = { exports: {} };
// Load the classic browser script independently of a parent package's ESM mode.
vm.runInThisContext('(function(module) {\n' +
  fs.readFileSync(path.join(__dirname, '../static/text-tools.js'), 'utf8') + '\n})',
  { filename: 'text-tools.js' })(testModule);
const { fixLayout, targetFor } = testModule.exports;

test('Hebrew and English map by physical key, including final letters', () => {
  assert.equal(fixLayout('akuo'), 'שלום');
  assert.equal(fixLayout('שלום'), 'akuo');
  assert.equal(fixLayout('hello world'), 'יקךךם \'םרךג');
  assert.equal(fixLayout(fixLayout('hello world')), 'hello world');
  assert.equal(fixLayout('ף'), ';');
  assert.equal(fixLayout('ףץךםן'), ';.loi');
});

test('unknown characters, emoji, whitespace and punctuation-only text survive', () => {
  assert.equal(fixLayout('AKUO 😀 123\n'), 'שלום 😀 123\n');
  assert.equal(fixLayout('123 !\n'), '123 !\n');
  assert.equal(fixLayout(''), '');
  assert.equal(fixLayout('שלום,'), "akuo'");
});

test('correction scope is only selected text or the entire unselected draft', () => {
  const el = { value: 'Keep teh word', selectionStart: 5, selectionEnd: 8 };
  assert.deepEqual(targetFor(el), { value: 'Keep teh word', start: 5, end: 8, text: 'teh' });
  el.selectionEnd = el.selectionStart;
  assert.deepEqual(targetFor(el), { value: el.value, start: 0, end: 13, text: el.value });
  el.selectionStart = 4; el.selectionEnd = 5;
  assert.deepEqual(targetFor(el), { value: el.value, start: 4, end: 5, text: ' ' });
});
