# Composer text tools

Open **Settings → Tools** and enable either **Keyboard layout correction** or
**Spelling and grammar correction**. Both are off by default and saved in this
browser. The buttons appear beside the microphone in the conversation composer
and Simple Home, including split panes and phone layouts.

- **⇄** flips QWERTY English ↔ standard Hebrew by physical key position, entirely
  offline. For example, `akuo` becomes `שלום`. This deliberately flips the text;
  it does not guess whether correctly typed words need changing.
- **A✓** fixes spelling and grammar while preserving the language and meaning.
  It makes a separate correction request; it never sends or resumes the active
  conversation. It uses the configured account's quota or API billing.

Select a passage to correct just that passage, or leave nothing selected to
correct the whole draft. Review the result, and use **Ctrl+Z / Cmd+Z** to undo.
Editing or switching the draft while spelling is running discards the result.

## Spelling backend

The default uses an already installed and signed-in **Claude Code**, model
`haiku`. It runs with tools and MCP disabled, no session persistence, and a
temporary working directory. Set `CCC_CLAUDE_BIN` for a nonstandard executable
location or `CCC_TEXT_TOOLS_MODEL` for another model supported by your account.
The spelling setting shows whether the command is available; command presence
does not check account authentication or model entitlement.

To use another model service, subscription, or local corrector, set
`CCC_TEXT_TOOLS_COMMAND` to a **JSON array of executable and arguments** before
starting CCC. That command receives the correction instructions plus the
selected text on **stdin** and must return only corrected text on **stdout**.
For example, `["python3", "/absolute/path/to/your-corrector.py"]`. CCC does not
invoke a shell or accept commands from the browser. Use a text-only adapter;
your command controls its own permissions, credentials, provider, and costs.

Restart the dashboard after changing environment configuration. Corrections
are capped at 20,000 characters, one concurrent command, and 60 seconds. Failures
keep the original draft and display a reason. No runtime dependencies are added.
