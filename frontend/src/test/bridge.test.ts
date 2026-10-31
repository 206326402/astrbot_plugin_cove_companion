import {afterEach, describe, expect, it, vi} from 'vitest';

afterEach(() => { delete window.AstrBotPluginPage; delete window.AstrBotPluginView; vi.resetModules(); });

describe('bridge request bodies', () => {
  it('preserves intentional empty values in settings POST requests', async () => {
    const apiPost = vi.fn(async () => ({status: 'ok', data: {saved: true}}));
    window.AstrBotPluginPage = {ready: async () => undefined, apiPost};
    const {post} = await import('../bridge');
    await post('settings', {excluded_terms: '', reference_playlist_ids: [], autodj_enabled: false, session_max_minutes: 0});
    expect(apiPost).toHaveBeenCalledWith('v1/settings', {excluded_terms: '', reference_playlist_ids: [], autodj_enabled: false, session_max_minutes: 0});
  });
});
