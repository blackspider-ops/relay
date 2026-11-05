# Sharing Relay

## GitHub and npx

The GitHub repository is enough for friends to install or try Relay:

```sh
npm install -g github:blackspider-ops/relay#v0.4.0
npx --yes github:blackspider-ops/relay#v0.4.0 --version
```

`npx` runs packages. It is not a separate publishing service. Publishing the
`relay-codex` package on npm would also make `npx relay-codex` available.

## Publish on npm

Only a maintainer needs to do this:

```sh
npm login
npm test
npm run check
npm pack --dry-run
npm publish --access public
```

Use an npm account with publishing access and two-factor authentication, or the
supported trusted-publishing setup. Package-name availability is checked again at
publication; the GitHub repository does not reserve the npm name.

After publishing, friends can use:

```sh
npx relay-codex setup --count 2
npm install -g relay-codex
```

## Homebrew

The release includes a formula under `Formula/`. It can be installed from this
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

Official references: [npm publishing](https://docs.npmjs.com/creating-and-publishing-unscoped-public-packages),
[npx](https://docs.npmjs.com/cli/v11/commands/npx/), and
[Homebrew taps](https://docs.brew.sh/How-to-Create-and-Maintain-a-Tap).
