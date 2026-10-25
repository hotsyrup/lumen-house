// Production composition has no review, storage, UI or execution imports.
// Host entrypoints must preserve their existing named runtime exports separately.
export function withDisabledHouseExchange(house) {
  if (!house || typeof house.fetch !== 'function') {
    throw new TypeError('House fetch implementation required');
  }
  return {
    fetch(request, env, ctx) {
      return house.fetch(request, env, ctx);
    },
  };
}
