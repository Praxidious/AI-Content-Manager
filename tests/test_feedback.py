import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from src.feedback import FeedbackStore, clip_id, import_outputs
from src.review import create_app, make_preview, preview_path
from src.analyze import analyze_transcript
from src.config import AnalysisConfig
from tests.test_pipeline import FakeProvider, segment


class FeedbackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = FeedbackStore(self.root / 'feedback.sqlite3')
        self.source = {'fingerprint': 'source-a', 'filename': 'vod.mkv', 'source_path': str(self.root / 'vod.mkv')}

    def candidate(self, start=10, **extra):
        return dict(core_start=start, core_end=start+15, recommended_start=start, recommended_end=start+15,
                    content_tags=['CHAOS'], exact_transcript_evidence='hello chaotic funny reaction',
                    entertainment_score=8, tier='A', **extra)

    def add(self, start=10):
        c=self.candidate(start)
        return self.store.save_candidate(c, self.source, self.root)

    def test_persistence_and_rating_update(self):
        identifier=self.add()
        self.store.rate(identifier, 'good', ['funny'], 'Worth posting')
        reopened=FeedbackStore(self.store.path)
        self.assertEqual(reopened.get(identifier)['rating'], 'good')
        reopened.rate(identifier, 'bad', ['no payoff'], 'Changed my mind')
        self.assertEqual(self.store.get(identifier)['rating'], 'bad')
        with closing(self.store.connect()) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM feedback_events').fetchone()[0],2)

    def test_reasons_before_after_vote_and_note(self):
        identifier=self.add()
        self.store.rate(identifier,None,['great reaction'])
        self.assertIsNone(self.store.get(identifier)['rating'])
        self.store.rate(identifier,'s_tier',['great reaction','funny','funny'],'Morgan reaction')
        value=self.store.get(identifier)
        self.assertEqual(value['reasons'],['great reaction','funny'])
        self.assertEqual(value['note'],'Morgan reaction')

    def test_invalid_feedback_rejected(self):
        identifier=self.add()
        for rating,reasons in [('excellent',[]),('good',['invented']),('good','funny')]:
            with self.assertRaises(ValueError):self.store.rate(identifier,rating,reasons)
        with self.assertRaises(KeyError):self.store.rate('missing','good')
        with self.assertRaises(ValueError):self.store.rate(identifier,'good',note='x'*2001)

    def test_unrated_excluded_from_examples_and_summary(self):
        self.add()
        self.assertEqual(self.store.examples(self.candidate()),[])
        self.assertEqual(self.store.summary()['total_reviewed'],0)
        self.assertEqual(len(self.store.clips(only_unrated=True)),1)

    def test_positive_negative_examples_and_weights(self):
        ids={rating:self.add(start) for start,rating in enumerate(['good','s_tier','bad','never_again','maybe'],100)}
        for rating,identifier in ids.items():self.store.rate(identifier,rating)
        examples=self.store.examples(self.candidate())
        self.assertEqual([x['rating'] for x in examples],['s_tier','good','never_again','bad'])
        self.assertEqual([x['weight'] for x in examples],[2,1,2,1])
        self.assertEqual(len(self.store.examples(self.candidate(),per_side=100)),4)
        self.assertNotIn(ids['s_tier'],[x['clip_id'] for x in self.store.examples(self.candidate(),exclude_id=ids['s_tier'])])

    def test_unrelated_examples_excluded(self):
        identifier=self.add()
        self.store.rate(identifier,'good')
        self.assertEqual(self.store.examples({'core_start':0,'core_end':5,'content_tags':['STRATEGY'],
                                             'exact_transcript_evidence':'resource management strategy'}),[])

    def test_duplicate_ids_preserve_feedback_creation_and_snapshot(self):
        identifier=self.add()
        self.store.rate(identifier,'s_tier')
        old=self.store.get(identifier)
        candidate=self.candidate()
        candidate['recommended_start']=8
        candidate['entertainment_score']=4
        candidate['clip_id']='malicious-or-old-id'
        self.assertEqual(self.store.save_candidate(candidate,self.source,self.root),identifier)
        new=self.store.get(identifier)
        self.assertEqual(len(self.store.clips()),1)
        self.assertEqual(new['rating'],'s_tier')
        self.assertEqual(old['created_at'],new['created_at'])
        self.assertEqual(new['features']['entertainment_score'],8)
        self.assertEqual(new['metadata']['entertainment_score'],4)
        self.assertNotEqual(clip_id('different-vod',candidate),identifier)

    def test_review_order_stable_and_not_changed_by_voting(self):
        identifiers=[self.add(x) for x in (30,10,20)]
        before=[x['clip_id'] for x in self.store.clips()]
        self.assertEqual([x['start'] for x in self.store.clips()],[10,20,30])
        self.store.rate(identifiers[1],'good')
        self.assertEqual([x['clip_id'] for x in self.store.clips()],before)
        self.assertEqual(len(self.store.clips(only_unrated=True)),2)
        self.assertEqual(before,[x['clip_id'] for x in FeedbackStore(self.store.path).clips()])

    def test_summary_and_training_export(self):
        self.store.rate(self.add(1),'s_tier',['funny','great reaction'])
        self.store.rate(self.add(2),'bad',['boring gameplay'])
        self.add(3)
        summary=self.store.summary()
        self.assertEqual(summary['total_reviewed'],2)
        self.assertEqual(summary['distribution'],{'s_tier':1,'bad':1})
        self.assertIn(('funny',1),summary['positive_reasons'])
        self.assertIn(('boring gameplay',1),summary['negative_reasons'])
        path=self.root/'export.jsonl';self.store.export(path)
        rows=[json.loads(line) for line in path.read_text().splitlines()]
        self.assertEqual(len(rows),2)
        self.assertTrue(all(x['features'] and x['schema_version']==1 for x in rows))

    def test_legacy_import_idempotent(self):
        folder=self.root/'old-output';folder.mkdir()
        (folder/'source.json').write_text(json.dumps(self.source))
        (folder/'candidates.json').write_text(json.dumps({'candidates':[self.candidate()]}))
        self.assertEqual(import_outputs(self.store,self.root),1)
        identifier=self.store.clips()[0]['clip_id'];self.store.rate(identifier,'good')
        self.assertEqual(import_outputs(self.store,self.root),0)
        self.assertEqual(self.store.get(identifier)['rating'],'good')

    def test_preference_call_cache_generic_scores_and_feedback_changes(self):
        class Provider(FakeProvider):
            preference_calls=0
            def analyze(self,prompt,user,schema):
                if 'personal_preference_score' in schema.get('properties',{}):
                    self.preference_calls+=1
                    self.last_examples=json.loads(user)['editor_examples']
                    return json.dumps({'personal_preference_score':1.5,'preference_reason':'Similar strong reaction'})
                return super().analyze(prompt,user,schema)
        identifier=self.add(200)
        self.store.rate(identifier,'s_tier',['funny'])
        provider=Provider();prompts=Path(__file__).resolve().parents[1]/'prompts'
        def run():return analyze_transcript([segment(100,110,'hello')],AnalysisConfig(),prompts,self.root/'cache',
            provider=provider,feedback_store=self.store,source=self.source)
        first=run();second=run()
        self.assertEqual(provider.calls,3)
        self.assertEqual(provider.preference_calls,1)
        candidate=first.candidates[0]
        self.assertEqual(candidate['generic_quality_score'],8.5)
        self.assertEqual(candidate['personal_preference_score'],1.5)
        self.assertEqual(candidate['final_rank_score'],10)
        self.assertEqual(candidate['tier'],'A')
        self.assertEqual(candidate['clip_id'],second.candidates[0]['clip_id'])
        self.store.rate(identifier,'never_again',['repetitive'])
        run()
        self.assertEqual(provider.calls,4)  # Discovery refreshes when editor guidance changes.
        self.assertEqual(provider.preference_calls,2)
        self.assertEqual(provider.last_examples[0]['weight'],2)

    def test_feedback_guides_discovery_without_inventing_positive_votes(self):
        self.store.rate(self.add(),'bad',['generic moment'])
        guidance=self.store.discovery_guidance([{'text':'hello chaotic funny reaction'}])
        self.assertIn('generic moment',guidance)
        self.assertIn('Do not invent positive preferences',guidance)
        self.assertIn('"bad": 1',guidance)

    def test_rejected_moment_matches_shifted_boundaries_only_same_vod(self):
        self.store.rate(self.add(),'bad',['no payoff'])
        self.assertIsNotNone(self.store.rejected_match(self.source,{'core_start':12,'core_end':27}))
        self.assertIsNone(self.store.rejected_match({'fingerprint':'other'},{'core_start':12,'core_end':27}))

    def test_completed_rescan_retires_old_moments_preserving_feedback(self):
        old=self.add(10);new=self.add(100)
        self.store.rate(old,'good')
        self.store.finish_source_run(self.source,{new})
        self.assertTrue(self.store.get(old)['metadata']['superseded'])
        self.assertEqual(self.store.get(old)['rating'],'good')
        self.assertFalse(self.store.get(new)['metadata']['superseded'])

    def test_preference_failure_retains_generic_ranking(self):
        class Provider(FakeProvider):
            def analyze(self,prompt,user,schema):
                if 'personal_preference_score' in schema.get('properties',{}):
                    return '{"personal_preference_score": "not a number"}'
                return super().analyze(prompt,user,schema)
        self.store.rate(self.add(200),'bad')
        outcome=analyze_transcript([segment(100,110,'hello')],AnalysisConfig(),
            Path(__file__).resolve().parents[1]/'prompts',self.root/'cache',
            provider=Provider(),feedback_store=self.store,source=self.source)
        self.assertEqual(outcome.candidates[0]['personal_preference_score'],0)
        self.assertEqual(outcome.candidates[0]['final_rank_score'],8.5)

    def test_rejected_candidate_has_identity_transcript_and_audio(self):
        class Provider(FakeProvider):
            def analyze(self,prompt,user,schema):
                result=json.loads(super().analyze(prompt,user,schema))
                if 'tier' in result:result['tier']='REJECT'
                return json.dumps(result)
        outcome=analyze_transcript([segment(100,110,'hello')],AnalysisConfig(),
            Path(__file__).resolve().parents[1]/'prompts',self.root/'cache',
            provider=Provider(),feedback_store=self.store,source=self.source,
            audio_signals=[{'start':100,'end':110,'relative_energy':5}])
        self.assertEqual(outcome.candidates,[])
        rejected=outcome.rejected[0]
        stored=self.store.get(rejected['clip_id'])
        self.assertEqual(stored['metadata']['tier'],'REJECT')
        self.assertTrue(stored['metadata']['transcript_excerpt'])
        self.assertTrue(stored['metadata']['audio_signals'])


class ReviewAPITests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.store=FeedbackStore(self.root/'feedback.sqlite3')
        c={'core_start':10,'core_end':20,'tier':'A','entertainment_score':8,'content_tags':['CHAOS'],'transcript_excerpt':[{'start':10,'text':'<script>alert(1)</script>'}]}
        self.identifier=self.store.save_candidate(c,{'source_path':str(self.root/'missing.mkv')},self.root)
        self.app=create_app(self.store,self.root/'previews');self.client=self.app.test_client()
        import re
        page=self.client.get('/').get_data(as_text=True)
        self.token=re.search(r'name="review-token" content="([^"]+)"',page)[1]
        self.headers={'X-Review-Token':self.token}

    def test_page_and_api(self):
        page=self.client.get('/')
        self.assertEqual(page.status_code,200)
        self.assertIn('NEVER AGAIN',page.get_data(as_text=True))
        self.assertIn("default-src 'self'",page.headers['Content-Security-Policy'])
        self.assertEqual(len(self.client.get('/api/clips').json),1)
        self.assertEqual(self.client.get('/api/summary').json['total_reviewed'],0)

    def test_source_filter_precedes_limit_and_survives_votes(self):
        for index in range(25):
            self.store.save_candidate({'core_start':index*30,'core_end':index*30+15,
                'tier':'S','entertainment_score':10}, {'fingerprint':'other'}, self.root)
        source = self.store.get(self.identifier)['source_key']
        response = self.client.get('/api/clips', query_string={'source':source})
        self.assertEqual([c['clip_id'] for c in response.json], [self.identifier])
        self.store.rate(self.identifier, 'good')
        self.assertEqual(self.client.get('/api/clips', query_string={'source':source}).json, [])
        self.assertEqual(self.client.get('/api/clips?source=unknown').json, [])
        self.assertEqual(self.client.get('/api/clips?source=').json, [])
        self.assertEqual(len(self.client.get('/api/clips').json), 20)

    def test_review_queue_minimum_is_ten_seconds(self):
        for length in (9.999, 10, 10.001):
            self.store.save_candidate({'core_start':100, 'core_end':100+length,'tier':'A','entertainment_score':8},
                                      {'fingerprint':str(length)},self.root)
        for url in ('/api/clips','/api/clips?unrated=1'):
            clips=self.client.get(url).json
            self.assertEqual(len(clips),3)
            self.assertTrue(all(c['end']-c['start']>=10 for c in clips))
        self.assertEqual(len(self.store.clips()),4)

    def test_top_twenty_remaining_clips_refill_after_votes(self):
        for index in range(25):
            self.store.save_candidate({'core_start':index*30,'core_end':index*30+15,
                'tier':'A','entertainment_score':7,'final_rank_score':8+index/100},
                {'fingerprint':'ranked'},self.root)
        top=self.client.get('/api/clips').json
        self.assertEqual(len(top),20)
        self.assertEqual([c['metadata']['final_rank_score'] for c in top],
                         [8+index/100 for index in range(24,4,-1)])
        self.store.rate(top[0]['clip_id'],'good')
        for url in ('/api/clips', '/api/clips?unrated=1'):
            remaining=self.client.get(url).json
            self.assertEqual(len(remaining),20)
            self.assertEqual([c['clip_id'] for c in remaining[:19]],[c['clip_id'] for c in top[1:]])
            self.assertEqual(remaining[-1]['metadata']['final_rank_score'],8.04)
            self.assertTrue(all(c['rating'] is None for c in remaining))

    def test_every_rating_stays_excluded_after_reprocessing_and_reopening(self):
        for index,rating in enumerate(('s_tier','good','maybe','bad','never_again')):
            candidate={'core_start':index*30,'core_end':index*30+15,'tier':'S','entertainment_score':10}
            source={'fingerprint':'reviewed'}
            identifier=self.store.save_candidate(candidate,source,self.root)
            self.store.rate(identifier,rating)
            self.assertEqual(self.store.save_candidate(candidate,source,self.root),identifier)
        reopened=create_app(FeedbackStore(self.store.path),self.root/'previews').test_client()
        self.assertEqual([c['clip_id'] for c in reopened.get('/api/clips').json],[self.identifier])
        self.assertEqual(reopened.get('/api/summary').json['total_reviewed'],5)

    def test_mediocre_rejected_unscored_and_duplicates_excluded(self):
        for index,fields in enumerate(({'tier':'B'},{'tier':'REJECT'},
                {'tier':'S','stage':'DEDUPLICATION'},{'tier':'A','entertainment_score':None},
                {'tier':'S','entertainment_score':float('nan')},{})):
            self.store.save_candidate({'core_start':30*index,'core_end':30*index+15,
                'entertainment_score':10,**fields},{'fingerprint':str(index)},self.root)
        self.assertEqual([c['clip_id'] for c in self.client.get('/api/clips').json],[self.identifier])

    def test_vote_reasons_updates_and_validation(self):
        url=f'/api/clips/{self.identifier}/feedback'
        self.assertEqual(self.client.post(url,json={'rating':'good'}).status_code,403)
        result=self.client.post(url,json={'rating':'s_tier','reasons':['great reaction'],'note':'Nice'},headers=self.headers)
        self.assertEqual(result.status_code,200)
        self.assertEqual(result.json['rating'],'s_tier')
        self.assertEqual(self.client.get('/api/clips?unrated=1').json,[])
        self.assertEqual(self.client.post(url,json={'rating':'invalid'},headers=self.headers).status_code,400)
        self.assertEqual(self.client.post('/api/clips/unknown/feedback',json={},headers=self.headers).status_code,404)

    def test_source_missing_still_allows_feedback(self):
        result=self.client.post(f'/api/clips/{self.identifier}/preview',json={},headers=self.headers)
        self.assertEqual(result.status_code,422)
        self.assertIn('Preview unavailable',result.json['error'])
        result=self.client.post(f'/api/clips/{self.identifier}/feedback',json={'rating':'maybe'},headers=self.headers)
        self.assertEqual(result.status_code,200)

    def test_preview_serves_ranges_and_does_not_expose_arbitrary_files(self):
        item=self.store.get(self.identifier)
        path=preview_path(item,self.root/'previews');path.parent.mkdir();path.write_bytes(b'0123456789')
        result=self.client.get(f'/api/clips/{self.identifier}/video',headers={'Range':'bytes=2-5'})
        self.assertEqual(result.status_code,206)
        self.assertEqual(result.data,b'2345');result.close()
        self.assertEqual(self.client.get('/api/clips/unknown/video').status_code,404)
        self.assertEqual(self.client.get('/api/clips',headers={'Host':'evil.example'}).status_code,400)

    def test_preview_cache_changes_with_bounds(self):
        item=self.store.get(self.identifier)
        old=preview_path(item,self.root);item['start']=8
        self.assertNotEqual(old,preview_path(item,self.root))


if __name__=='__main__':unittest.main()
