import unittest
from unittest.mock import Mock
import monitor

class TestMonitor(unittest.TestCase):
    def setUp(self):
        self.config={'keywords':['בוסטון טרייר','boston terrier'],'exclude_keywords':['למכירה']}
        self.source={'name':'Findog adoption','url':'https://www.findog.co.il/Dogs/ByCat/29/example?type=3'}

    def test_ignore_recommended_results(self):
        html='''<h2>בוסטון טרייר</h2><h3>לא נמצאו תוצאות מתאימות</h3><a href="/Dogs/Details/123">בוסטון טרייר למסירה</a>'''
        session=Mock();session.get.return_value.text=html
        self.assertEqual(monitor.fetch_findog(self.source,self.config,session),[])

    def test_pick_individual_listing(self):
        html='''<h2>בוסטון טרייר</h2><div><a href="/Dogs/Details/123">בוסטון טרייר למסירה</a></div><a href="/Dogs/ByCat/29">בוסטון טרייר</a>'''
        session=Mock();session.get.return_value.text=html
        items=monitor.fetch_findog(self.source,self.config,session)
        self.assertEqual(len(items),1)
        self.assertTrue(items[0]['url'].endswith('/Dogs/Details/123'))

    def test_exclusion(self):
        self.assertFalse(monitor.match('בוסטון טרייר למכירה',self.config))
        self.assertTrue(monitor.match('בוסטון טרייר למסירה',self.config))

if __name__=='__main__':unittest.main()
