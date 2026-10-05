export function assetInlineLimit(filePath: string): false | undefined {
  return /\.(woff2?|ttf|otf)(?:[?#].*)?$/i.test(filePath) ? false : undefined
}
