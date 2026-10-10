import {createRequire} from 'node:module'
import {fileURLToPath} from 'node:url'
import {rolldown} from 'rolldown'

export async function componentModule(file, overrides = {}) {
  const url = new URL(`../${file}`, import.meta.url)
  const bundle = await rolldown({
    input: fileURLToPath(url),
    external: ['react', 'react/jsx-runtime', 'lucide-react', ...Object.keys(overrides)],
    transform: {jsx: {runtime: 'automatic'}},
  })
  const {output} = await bundle.generate({format: 'cjs', exports: 'named'})
  await bundle.close()
  const require = createRequire(url)
  const module = {exports: {}}
  new Function('require', 'module', 'exports', output[0].code)(
    name => name in overrides ? overrides[name] : require(name), module, module.exports,
  )
  return module.exports
}
