import json,tempfile,unittest
from datetime import datetime,timezone
from pathlib import Path
from marketdesk.sectors import sector_group
from marketdesk.history_rules import is_financial
from marketdesk.storage import init,connect
from marketdesk.quality import audit


class SectorGroups(unittest.TestCase):
 def test_provider_spellings_share_one_group(self):
  self.assertEqual({sector_group(x) for x in ('Bank','Banks','Finance','NBFC','Housing Finance')},{'Financials'})
  self.assertEqual({sector_group(x) for x in ('Oil Exploration','Refineries','Gases & Fuels')},{'Energy'})
  self.assertEqual(sector_group(' IT-Software '),'Information Technology')
 def test_reits_are_real_estate_whatever_the_label(self):
  self.assertEqual(sector_group('Investment',symbol='BIRET'),'Real Estate')
  self.assertEqual(sector_group('Investment','EMBASSY OFFICE PARKS REIT'),'Real Estate')
  self.assertEqual(sector_group('Investment'),'Financials')
 def test_unknown_names_pass_through(self):
  self.assertEqual(sector_group('Diversified'),'Diversified');self.assertEqual(sector_group(None),'Unknown')


class FinancialSectors(unittest.TestCase):
 def test_lender_and_insurer_labels_stay_financial(self):
  for label in ('Bank','Banks','Finance','Housing Finance','NBFC','Insurance','Asset Management','Stock Broking'):
   self.assertTrue(is_financial(label),label)
 def test_brokers_exchanges_and_investment_companies_are_financial(self):
  for label,symbol in (('Investment','IIFL'),('Investment','KFINTECH'),('Stock/ Commodity Brokers','NUVAMA'),('Financial Services','MCX'),(' investment ',None)):
   self.assertTrue(is_financial(label,symbol),(label,symbol))
 def test_reits_and_operating_businesses_are_not_financial(self):
  self.assertFalse(is_financial('Real Estate Investment Trusts'));self.assertFalse(is_financial('Real Estate Investment Trusts','BAGMANE'))
  for symbol in ('BIRET','EMBASSY','RADIANTCMS','KAMAHOLD'):self.assertFalse(is_financial('Investment',symbol),symbol)
 def test_other_labels_are_not_financial(self):
  for label in ('Ratings','Realty','IT - Software','Power','',None):self.assertFalse(is_financial(label),label)
 def test_audit_flags_by_label_and_symbol(self):
  with tempfile.TemporaryDirectory() as folder:
   path=Path(folder)/'db';init(path)
   with connect(path) as db:
    for symbol,sector in (('IIFL','Investment'),('MCX','Financial Services'),('EMBASSY','Investment'),('CRISIL','Ratings')):
     db.execute('INSERT INTO instruments VALUES(?,?,?,?,?)',(symbol,'NSE_EQ|'+symbol,symbol,symbol,sector))
   summary=audit(path,datetime(2026,10,21,5,tzinfo=timezone.utc))
   with connect(path) as db:flags={r[0]:json.loads(r[1])['financial_sector'] for r in db.execute('SELECT symbol,report FROM quality')}
  self.assertEqual(flags,{'IIFL':True,'MCX':True,'EMBASSY':False,'CRISIL':False});self.assertEqual(summary['financial_sector_stocks'],2)


if __name__=='__main__':unittest.main()
