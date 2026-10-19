import copy,unittest
from marketdesk.fallbacks import nse_annual
from marketdesk.growth_repair import checked_pair
from test_fallbacks import fixture

class GrowthRepairTests(unittest.TestCase):
 def test_current_filing_comparative_needs_no_older_index_and_retains_offset(self):
  import tempfile,hashlib
  from pathlib import Path
  from unittest.mock import patch
  from marketdesk.growth_repair import recover,extract_saved,retain_growth
  parts=fixture().split('</table>');parts[1]+='<tr><td>Revenue from operations</td><td>2000</td><td>1500</td></tr><tr><td>Total profit (loss) for period</td><td>700</td><td>600</td></tr>';raw='</table>'.join(parts).encode()
  with tempfile.TemporaryDirectory() as tmp,patch('marketdesk.growth_repair.ROOT',Path(tmp)):
   folder=Path(tmp)/'data/filings/TEST';folder.mkdir(parents=True);sha=hashlib.sha256(raw).hexdigest();(folder/(sha+'.html')).write_bytes(raw)
   record={'raw_sha256':sha,'source_url':'url','observed_at':'2026-10-13T00:00:00+00:00'};current=extract_saved('TEST','INE000A01010',record);details={'latest':current,'summary':{},'selected_source':'nse_audited_filing'}
   proof=recover('TEST','INE000A01010',details);self.assertEqual(proof['prior']['year_offset'],-1);self.assertEqual(proof['prior']['observed_at'],record['observed_at']);self.assertAlmostEqual(proof['summary']['revenue_growth_yoy'],(20/15-1)*100)
   retained=retain_growth(copy.deepcopy(details),{'growth_repair':proof},'TEST','INE000A01010');self.assertEqual(retained['summary'],proof['summary'])
   wrong=copy.deepcopy(proof);wrong['prior']['year_offset']=0;self.assertEqual(retain_growth(copy.deepcopy(details),{'growth_repair':wrong},'TEST','INE000A01010')['summary'],{})
 def test_income_parse_does_not_require_comparative_balance(self):
  html=fixture();parts=html.split('</table>');parts[1]=parts[1].replace('</table>','')+'<tr><td>Revenue from operations</td><td>2000</td><td>1500</td></tr><tr><td>Total profit (loss) for period</td><td>700</td><td>600</td></tr>'
  # Remove balance entirely: only the explicit audited income column is usable.
  income='</table>'.join(parts[:2])+'</table>'
  prior=nse_annual(income,'TEST','INE000A01010','url',year_offset=-1,income_only=True)
  self.assertEqual(prior['revenue'],15);self.assertEqual(prior['net_profit'],6)
  with self.assertRaises(ValueError):nse_annual(income,'TEST','INE000A01010','url',year_offset=-1)
 def test_legacy_actual_rupees_and_textual_dates(self):
  html=fixture().replace('<tr><td>Level of rounding used in financial results</td><td>Lakhs</td></tr>','').replace('<td>INR</td>','<td>INR (in Actuals)</td>').replace('01-04-','01-Apr-').replace('31-03-','31-Mar-')
  row=nse_annual(html,'TEST','INE000A01010','url',income_only=True)
  self.assertAlmostEqual(row['profit_before_tax'],.0001)
  with self.assertRaises(ValueError):nse_annual(html.replace('INR (in Actuals)','INR'),'TEST','INE000A01010','url',income_only=True)
 def test_income_mode_preserves_annual_identity_basis_and_audit_checks(self):
  for text in (fixture().replace('INE000A01010','OTHER'),fixture().replace('01-04-2025','01-01-2026'),fixture().replace('Audited','Unaudited'),fixture().replace('INR','USD')):
   with self.assertRaises(ValueError):nse_annual(text,'TEST','INE000A01010','url',income_only=True)
 def test_growth_requires_matching_anchors_and_comparable_positive_base(self):
  current={'period':'Mar 2026','period_end':'2026-03-31','statement_type':'consolidated','units_in':'crore','profit_before_tax':10,'finance_costs':1,'revenue':120,'net_profit':12,'observed_at':'2026-10-13T00:00:00+00:00'}
  prior={**current,'period':'Mar 2025','period_end':'2025-03-31','revenue':100,'net_profit':10}
  details={'latest':copy.deepcopy(current),'summary':{}}
  summary=checked_pair(details,current,prior);self.assertAlmostEqual(summary['revenue_growth_yoy'],20);self.assertEqual(details['summary'],{})
  for key,value in [('statement_type','standalone'),('units_in','lakh'),('period_end','2024-03-31'),('revenue',None)]:
   with self.assertRaises(ValueError):checked_pair(details,current,{**prior,key:value})
  # A zero or negative prior base keeps both verified years and leaves growth undefined.
  from marketdesk.quality import undefined_growth
  for value in (0,-1):
   summary=checked_pair(details,current,{**prior,'net_profit':value})
   self.assertIsNone(summary['net_profit_growth_yoy']);self.assertEqual(summary['growth_inputs']['net_profit']['prior'],value)
   self.assertTrue(undefined_growth(summary,'net_profit'));self.assertNotIn('net_profit',summary['growth_sources'])
  with self.assertRaises(ValueError):checked_pair(details,{**current,'finance_costs':9},prior)

 def test_retained_growth_requires_unchanged_hash_and_current_context(self):
  import tempfile,hashlib,re
  from pathlib import Path
  from unittest.mock import patch
  from marketdesk.growth_repair import extract_saved,retain_growth
  html=fixture().replace('<td>Finance costs</td>', '<td>Finance costs</td>')
  addition='<tr><td>Revenue from operations</td><td>2000</td><td>1500</td></tr><tr><td>Total profit (loss) for period</td><td>700</td><td>600</td></tr>'
  parts=html.split('</table>');parts[1]+=addition;html='</table>'.join(parts)
  old=re.sub(r'202[456]',lambda m:str(int(m[0])-1),html)
  with tempfile.TemporaryDirectory() as tmp,patch('marketdesk.growth_repair.ROOT',Path(tmp)):
   folder=Path(tmp)/'data/filings/TEST';folder.mkdir(parents=True)
   reports=[]
   for text in (html,old):
    raw=text.encode();sha=hashlib.sha256(raw).hexdigest();(folder/(sha+'.html')).write_bytes(raw)
    reports.append(extract_saved('TEST','INE000A01010',{'raw_sha256':sha,'source_url':'url','observed_at':'2026-10-13T00:00:00+00:00'}))
   previous={'growth_repair':{'status':'verified','symbol':'TEST','isin':'INE000A01010','current':reports[0],'prior':reports[1]}}
   base={'latest':reports[0],'summary':{},'selected_source':'nse_audited_filing'}
   self.assertIn('revenue_growth_yoy',retain_growth(copy.deepcopy(base),previous,'TEST','INE000A01010')['summary'])
   bad=copy.deepcopy(base);bad['latest']['period']='Mar 2027';self.assertEqual(retain_growth(bad,previous,'TEST','INE000A01010')['summary'],{})
   (folder/(reports[1]['raw_sha256']+'.html')).write_bytes(b'changed')
   self.assertEqual(retain_growth(copy.deepcopy(base),previous,'TEST','INE000A01010')['summary'],{})
