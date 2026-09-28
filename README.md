# GitHub Sync

A small Windows app that shows, side by side, **your GitHub repositories** and **the project folders on this PC**. It lets you pull, push, clone or publish each one with a button click, so you don't need the terminal.

It's meant for keeping several PCs (for example, a home laptop and a work desktop) in step through GitHub.

## What it shows

| Colour | Meaning | Button to use |
|---|---|---|
| Green | In sync | – |
| Blue | GitHub has newer commits (made on your other PC) | **Pull** |
| Orange | This PC has commits that aren't on GitHub | **Push** |
| Yellow | Uncommitted edits on this PC | **Push** (asks for a short description) |
| Red | Diverged (changes on both sides), or an error | **Sync** |
| Grey | The repo is on GitHub but not on this PC | **Clone to this PC** |
| Purple | The folder is only on this PC | **Publish to GitHub** |

A local folder is matched to its GitHub repo through its git remote. So a folder named `Strut and Tie 2` is still matched correctly to `PileCapTruss`.

* **Sync** runs a pull and then a push. If the same lines were changed on both PCs, it stops, changes nothing and tells you.
* **Pull all behind** and **Clone all missing** apply to every row at once.
* To hide repos you don't want on this PC, right-click them and choose **Ignore on this PC**. Hidden repos appear under the **Show: Ignored** filter.
* **File → Change projects folder…** points the app at a different folder. This setting is saved per PC in `%APPDATA%\GitHubSync\config.json`.

## Setup on a new PC (one time)

1. Install **Git for Windows**: https://git-scm.com
2. Install **Python 3** (tick "Add to PATH"): https://python.org
3. Install the **GitHub CLI**: https://cli.github.com
4. Put this folder inside your projects folder (e.g. `C:\Created Apps & Programs\Github-sync`).
5. Double-click `github_sync.pyw`. If you aren't signed in, the app offers to sign you in.
6. Use **File → Create desktop shortcut** to add a desktop shortcut.
