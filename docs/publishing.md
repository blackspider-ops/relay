# Sharing Relay

Relay is published on npm as [relay-codex](https://www.npmjs.com/package/relay-codex).
Friends can run `npx relay-codex` or install it with `npm install -g relay-codex`.

## GitHub and npx

The GitHub repository is enough for friends to install or try Relay:

```sh
npm install -g github:blackspider-ops/relay#v0.5.2
npx --yes github:blackspider-ops/relay#v0.5.2 --version
```

`npx` runs packages; it is not a separate publishing service. Publishing
`relay-codex` on npm also makes `npx relay-codex` available.

## Publish on npm

The repository includes `.github/workflows/publish.yml`. Configure a trusted
publisher for `relay-codex` using GitHub user `blackspider-ops`, repository `relay`,
and workflow filename `publish.yml`. Select direct `npm publish` permission if
releases should publish automatically. No long-lived publishing token is needed.
A new trusted publisher must complete its first publish within npm's validation
window. Run the workflow for an existing reviewed version tag, or publish the
GitHub release; the workflow runs checks before publishing. After the first
successful trusted publish, set the repository variable
`NPM_TRUSTED_PUBLISHING_READY=true` to enable automatic release publishing. Until
then, releases do not launch a publishing attempt that requires authentication.

Initial trust setup requires npm account verification. Interactive local
`npm publish` also requires verification when package 2FA applies. Installing and
running the public package does not require npm sign-in. An expired publishing
prompt should be restarted once, rather than repeatedly running `npm login`.

For a manual release, review and publish the exact packed artifact:

```sh
npm test
npm run check
npm pack --ignore-scripts
npm publish ./relay-codex-VERSION.tgz --access public
```

After publishing, friends can use:

```sh
npx relay-codex setup --count 2
npm install -g relay-codex
```

## Homebrew

The repository includes a formula under `Formula/`. It can be installed from this
repository as a personal tap; it does not need acceptance into Homebrew's main
collection. The formula must point to a tagged release archive with its actual
SHA-256 checksum.

```sh
brew tap blackspider-ops/relay https://github.com/blackspider-ops/relay
brew install blackspider-ops/relay/relay-codex
```

For a new release, update the package and Python versions together, run the checks,
tag it, upload the archive, then update the formula URL and checksum. Keep the
previous release available for users who need to roll back.

Official references: [trusted publishing](https://docs.npmjs.com/trusted-publishers/),
[package publishing verification](https://docs.npmjs.com/requiring-2fa-for-package-publishing-and-settings-modification/),
[npm publishing](https://docs.npmjs.com/creating-and-publishing-unscoped-public-packages),
[npx](https://docs.npmjs.com/cli/v11/commands/npx/), and
[Homebrew taps](https://docs.brew.sh/How-to-Create-and-Maintain-a-Tap).
