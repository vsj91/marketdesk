import unittest
from marketdesk.sectors import sector_group


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


if __name__=='__main__':unittest.main()
