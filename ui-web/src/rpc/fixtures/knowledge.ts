/* The knowledge bases, which this page has none of.
 *
 * The one domain the offline shell never had a fixture for: `sources.knowledge`
 * was the live layer's alone, so opening the page with no gateway reached for a
 * source that was not there. It answers an empty library now -- which is what
 * a fresh install has, and a page the reader can look at rather than a failure.
 */

import type { FixtureEnv, Fixtures } from '../fixtureTransport'

export interface KnowledgeFixture {
  fixtures: Fixtures
}

export function createKnowledge(_env: FixtureEnv): KnowledgeFixture {
  return {
    fixtures: {
      /* `provider` rides with the model: a base is embedded through the
         provider that serves it, and a model id does not name one. */
      'knowledge.status': () => ({ configured: false, model: '', provider: '' }),
      'knowledge.bases.list': () => ({ bases: [] }),
      'knowledge.documents.list': () => ({ documents: [] }),
      /* `by_keyword` is what a search fell back to when the embedding
         endpoint could not be reached, so a reader can tell a keyword hit
         from a semantic one. Empty here, like the hits it accompanies. */
      'knowledge.search': () => ({ hits: [], by_keyword: [] }),
    },
  }
}
