# Committing with the hardware signing key

Commits in this repo are signed with an SSH hardware key (YubiKey):

```
commit.gpgsign = true
gpg.format     = ssh
user.signingkey = ~/.ssh/id_ed25519_sk_git.pub   # ED25519-SK
```

The key is **not** loaded in `ssh-agent` (the agent only holds the plain ED25519
auth key), so `git commit` invokes `ssh-keygen -Y sign`, which talks to the FIDO
device directly. It requires **a button press on the YubiKey — no passphrase.**

## The failure mode

A signed commit run in the foreground from a tool call fails like this:

```
error: Signing file /var/folders/.../.git_signing_buffer_tmpXXXX
Confirm user presence for key ED25519-SK SHA256:Nxcb...
Couldn't sign message: incorrect passphrase supplied to decrypt private key?
fatal: failed to write commit object
```

The `incorrect passphrase` wording is a red herring — it is `ssh-keygen`'s generic
error when `ssh-sk-helper` returns a failure. What actually happened is the
`Confirm user presence` prompt timed out because nobody tapped the key: the
message is buffered in the tool output and is not seen live, so there is no cue
to press it.

## The fix: commit in the background, and ask for the tap immediately

Run the commit as a **background** Bash command. It detaches instead of blocking,
so the request for a tap can go out while the key is already blinking:

```
Bash(command="cd <repo> && git commit -F - <<'EOF'\n<message>\nEOF",
     run_in_background=true)
```

This buys a window, it does not remove the deadline: `ssh-sk-helper` gives up
after roughly 15–25 seconds and the commit fails with the same misleading error.
So:

- Ask for the tap in the **very next message**, before any other tool call.
  Do not queue up file reads or status checks first — that spends the window.
- Do not stage more work while a signature is pending.
- A timeout is not a broken setup. The staged index is untouched, so just re-run
  the identical command and ask again.

Verify afterwards:

```bash
git log --show-signature --oneline -1
```

Expect `Good "git" signature with ED25519-SK key SHA256:Nxcb...`. The trailing
`No principal matched.` is only about `gpg.ssh.allowedSignersFile` not listing
the key; the signature itself is valid.

The same applies to any other signing operation: `git tag -s`, `git commit
--amend`, `git rebase` over signed commits, and `git merge -S`.

## Do not

Do not fall back to `-c commit.gpgsign=false`. Signing here is deliberate
(see `~/Work/Code/yubikey-setup`); an unsigned commit has to be amended later to
fix. Ask before deviating.

## Alternative

Loading the SK key into the agent once per session also works and makes
foreground commits succeed (still one tap per signature):

```bash
ssh-add ~/.ssh/id_ed25519_sk_git
```
