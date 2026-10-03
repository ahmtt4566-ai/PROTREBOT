import {hasPrivateMarker, jsxAttribute, passwordType} from './private-field-helpers.mjs'

export default {
  meta: {
    type: 'problem',
    docs: {description: 'Require an explicit private marker on password inputs, including reveal toggles'},
    schema: [],
    fixable: 'code',
    messages: {missing: 'Password inputs must explicitly declare data-private="true".'},
  },
  create(context) {
    return {
      JSXOpeningElement(node) {
        if (node.name.type !== 'JSXIdentifier' || node.name.name !== 'input' ||
            !passwordType(jsxAttribute(node, 'type')?.value) || hasPrivateMarker(node)) return
        context.report({
          node,
          messageId: 'missing',
          fix(fixer) {
            const marker = jsxAttribute(node, 'data-private')
            return marker ? fixer.replaceText(marker, 'data-private="true"') : fixer.insertTextAfter(node.name, ' data-private="true"')
          },
        })
      },
    }
  },
}
