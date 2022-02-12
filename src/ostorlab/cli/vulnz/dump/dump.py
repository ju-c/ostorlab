"""Vulnz dump command."""
import logging
import click


from ostorlab.cli import console as cli_console
from ostorlab.cli.vulnz import vulnz
from ostorlab.cli import dumpers
from ostorlab.runtimes.local.models import models


console = cli_console.Console()

logger = logging.getLogger(__name__)


@vulnz.command(name='dump')
@click.option('--scan-id', '-s', 'scan_id', help='Id of the scan.', required=True)
@click.option('--output', '-o', help='Output file path', required=False, default='/output.json')
@click.option('--format', '-f', 'output_format', help='Output format', required=False, type=click.Choice(['json', 'csv']), default='json')
def dump(scan_id: int, output: str, output_format: str) -> None:
    """Dump found vulnerabilities of a scan in a specific format."""
    database = models.Database()
    session = database.session
    vulnerabilities = session.query(models.Vulnerability).filter_by(scan_id=scan_id).\
        order_by(models.Vulnerability.title).all()

    vulnz_list = {
        'vulnerabilities': []
    }
    for vulnerability in vulnerabilities:
        vulnz_list['vulnerabilities'].append({
            'id': str(vulnerability.id),
            'risk_rating': vulnerability.risk_rating.value,
            'cvss_v3_vector': vulnerability.cvss_v3_vector,
            'title': vulnerability.title,
            'short_description': vulnerability.short_description
        })

    if output_format=='json':
        dumper = dumpers.VulnzJsonDumper(output, vulnz_list)
    if output_format=='csv':
        fieldnames = ['id', 'title', 'risk_rating', 'cvss_v3_vector', 'short_description']
        dumper = dumpers.VulnzCsvDumper(output, vulnz_list, fieldnames)

    try:
        dumper.dump()
    except FileNotFoundError as e:
        console.error(f'No such file or directory: {output}')
        raise click.exceptions.Exit(2) from e

    console.success(f'{len(vulnerabilities)} Vulnerabilities saved to  : {output}')
