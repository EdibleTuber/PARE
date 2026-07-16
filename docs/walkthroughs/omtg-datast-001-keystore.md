# Walkthrough — OMTG_DATAST_001_KeyStore

A worked solve of the OWASP **OMTG_DATAST_001_KeyStore** test case (from the
`sg.vp.owasp_mobile.omtg_android` "Attack me if u can" app), driven through PARE's
Frida worker plus a small raw-Frida script. It doubles as a reference for the
kind of runtime instrumentation PARE is built to make conversational.

> Authorized lab use only: a deliberately-vulnerable app on your own emulator.

## What the challenge is

This is the **"best-practice" KeyStore test case**, not a hidden-flag hunt. The
activity (`sg.vp.owasp_mobile.OMTG_Android.OMTG_DATAST_001_KeyStore`) has a Clear
Text field and ENCRYPT / DECRYPT buttons. From the decompiled bytecode
(`base.apk` → `dexdump`):

- `createNewKeys()` generates an **RSA-2048 keypair in `AndroidKeyStore`** under
  alias **`"Dummy"`** (subject `CN=Sample Name, O=Android Authority`). The private
  key is **non-exportable** and lives in `/data/misc/keystore/persistent.sqlite`.
- `encryptString(alias)` loads the **public** key by alias, reads the plaintext
  from the Clear Text `EditText`, encrypts with `RSA/ECB/PKCS1Padding` (provider
  `AndroidOpenSSL`) via a `CipherOutputStream`, and Base64-encodes the result into
  the middle field.
- `decryptString(alias)` loads the **private** key, `Base64.decode`s the
  ciphertext, runs it through a `CipherInputStream`, then builds
  `new String(bytes, "UTF-8")` into the Decrypted field.
- Both buttons call `…String("Dummy")` — **`"Dummy"` is the key alias, not a
  secret.** The `"12345678"` visible in `strings` is a `Log.v("test log: …")`
  **red herring**, unused by the crypto.

**Objective:** demonstrate that a runtime attacker recovers the plaintext *even
though the private key cannot be exported* — because the app itself can decrypt,
so code running in its process can too. The KeyStore protects the **key**, not the
**data in use**.

## Key facts (grounded in the app)

| Property | Value |
|---|---|
| Package (app id) | `sg.vp.owasp_mobile.omtg_android` |
| Java package (classes) | `sg.vp.owasp_mobile.OMTG_Android` (note the case difference) |
| Activity | `…OMTG_Android.OMTG_DATAST_001_KeyStore` |
| Key alias | `Dummy` |
| Transform | `RSA/ECB/PKCS1Padding`, provider `AndroidOpenSSL` |
| Key store | `AndroidKeyStore`, backed by `/data/misc/keystore/persistent.sqlite` |
| Hidden flag? | None — the "secret" is whatever you type |

> The app-id-vs-Java-package **case difference** is why a naive class filter of
> `sg.vp.owasp_mobile.omtg_android` returns nothing. PARE's `enumerate_classes`
> now matches case-insensitively, so the lowercase filter works.

## Part A — Map it with PARE

1. `/apps` → find `sg.vp.owasp_mobile.omtg_android`.
2. `/attach <pid>` (or `/attach sg.vp.owasp_mobile.omtg_android`). PARE now
   defaults later tool calls to this most-recent live session, so you don't have
   to restate the `session_id`.
3. On the device, open the **OMTG_DATAST_001_KeyStore** test so its classes load
   (classes load lazily — you must be on the screen).
4. `enumerate_classes` with filter `omtg_datast_001_keystore`
   (case-insensitive) → the class appears as
   `…OMTG_Android.OMTG_DATAST_001_KeyStore`.
5. `enumerate_methods` on that class → `createNewKeys`, `encryptString`,
   `decryptString`, `onCreate`.
6. `java_hook` on `encryptString` and `decryptString` → confirms both are invoked
   with the argument `"Dummy"` (the key alias). This maps the flow — **but note
   the plaintext is not in the method args**; it is read from the `EditText`
   inside the method.

## Part B — Capture the plaintext (the solve)

Because the cleartext flows through **framework** classes
(`EditText` → `CipherOutputStream`/`Cipher`/`Base64`, and back via
`new String(...)`), you intercept the **data path**, not the key. PARE's current
`java_hook` observes only *app-declared* method **arguments**, so this step uses a
raw Frida script (see [Capability gap](#capability-gap-in-pare)).

```js
// Attach to the app, load this, then press ENCRYPT / DECRYPT in the app.
Java.perform(() => {
  const S = Java.use('java.lang.String');
  const u8 = b => S.$new(b, 'UTF-8').toString();

  // cleartext entering encryption
  Java.use('javax.crypto.CipherOutputStream').write.overload('[B')
    .implementation = function (b) {
      console.log('[plaintext→encrypt]', u8(b));
      return this.write(b);
    };

  // recovered cleartext after decryption
  const ctor = S.$init.overload('[B', 'int', 'int', 'java.lang.String');
  ctor.implementation = function (b, o, l, cs) {
    const r = ctor.call(this, b, o, l, cs);
    console.log('[decrypt→plaintext]', this.toString());
    return r;
  };
});
```

Pressing ENCRYPT prints the cleartext as it enters the cipher; pressing DECRYPT
prints the recovered cleartext as the app rebuilds the string. That is the proof:
the plaintext is fully recoverable at runtime despite the non-exportable key.

**No-hook alternative:** the key is usable by the process, so you can also reuse
the app's own `decryptString` against any captured ciphertext, or inspect
`/data/misc/keystore/persistent.sqlite` to confirm the key is present but
non-exportable.

## Findings (MSTG-style)

- **Good practice observed:** the private key is generated in and non-exportable
  from the Android KeyStore.
- **Residual risk:** runtime instrumentation (Frida) recovers the plaintext,
  because the decryption capability lives in the app process. Sensitive data
  should be minimized in memory; if this matters for the app's threat model, add
  instrumentation/root detection and reduce the window plaintext is live.

## Capability gap in PARE

This challenge cannot be finished with PARE's tools alone, which is a useful
signal. Today `java_hook`:

- hooks only **app-declared** methods (not framework classes like
  `CipherOutputStream` or `String`), and
- logs **arguments only** — not return values, instance fields, or the values a
  method writes elsewhere (e.g. the decrypted `EditText`).

So PARE reveals the alias and the flow, but the actual plaintext capture drops to
a raw Frida script. Closing this gap (framework-method hooks + return-value /
field capture) is tracked as a follow-up.

## Reproduction notes

- Verified against `emulator-5554` (x86_64), frida-server 17.9.11, OMTG at
  pid 4230, using pare-frida-mcp's own bundled agent for enumeration/hooking.
- The activity is **not exported**, so it cannot be launched with shell
  `am start` (SecurityException); navigate to it in the app UI, or start it from
  inside the app's own uid via Frida.
