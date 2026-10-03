export const jsxAttribute = (node, name) => node.attributes.find(attribute =>
  attribute.type === 'JSXAttribute' && attribute.name.name === name)

export function literalValue(node) {
  if (!node) return undefined
  if (node.type === 'JSXExpressionContainer') return literalValue(node.expression)
  if (node.type === 'Literal' || node.type === 'StringLiteral' || node.type === 'BooleanLiteral') return node.value
  return undefined
}

export function passwordType(node) {
  if (!node) return false
  if (literalValue(node) === 'password') return true
  if (node.type === 'JSXExpressionContainer' || node.type === 'TSAsExpression' || node.type === 'TSSatisfiesExpression') return passwordType(node.expression)
  if (node.type === 'ConditionalExpression') return passwordType(node.consequent) || passwordType(node.alternate)
  if (node.type === 'LogicalExpression') return passwordType(node.left) || passwordType(node.right)
  return false
}

export function hasPrivateMarker(node) {
  const value = literalValue(jsxAttribute(node, 'data-private')?.value)
  return value === 'true' || value === true
}
