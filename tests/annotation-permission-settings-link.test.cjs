const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const app=fs.readFileSync('static/app.js','utf8');
function helper(win){
 const start=app.indexOf('  function annPermissionSettingsAction(');
 assert.notEqual(start,-1,'annPermissionSettingsAction exists');
 const end=app.indexOf('  function showOpToast(',start);
 const ctx=vm.createContext({window:win});
 vm.runInContext(app.slice(start,end),ctx);return ctx.annPermissionSettingsAction;
}
test('an Accessibility denial links to the Accessibility pane, even when Screen Recording is also named',()=>{
 const win={location:{href:''}};
 const action=helper(win)('macOS denied Accessibility access to the CCC server process (osascript/System Events, -25211); rect (1,2,3,4) does not intersect any displays (capture failed; check Screen Recording permission, window focus, or region bounds)');
 assert.equal(action.label,'Open Accessibility settings');
 action.onClick();
 assert.equal(win.location.href,'x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility');
});
test('a Screen Recording hint alone links to the Screen Recording pane',()=>{
 const win={location:{href:''}};
 const action=helper(win)('capture failed; check Screen Recording permission, window focus, or region bounds');
 assert.equal(action.label,'Open Screen Recording settings');
 action.onClick();
 assert.equal(win.location.href,'x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture');
});
test('warnings that name no permission get no action',()=>{
 assert.equal(helper({location:{}})('No screenshot. Use the Screen button for a manual region capture.'),undefined);
});
test('the no-screenshot toast passes the settings action',()=>{
 assert.match(app,/showOpToast\('Annotation saved \(no screenshot\): ' \+ warn, 'error',\s*annPermissionSettingsAction\(warn\)\)/);
});
