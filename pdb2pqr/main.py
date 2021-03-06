"""Perform functions related to _main_ execution of PDB2PQR.

This module is intended for functions that directly touch arguments provided at
the invocation of PDB2PQR.  It was created to avoid cluttering the __init__.py
file.
"""
import logging
import argparse
from collections import OrderedDict
from tempfile import NamedTemporaryFile
from pathlib import Path
from math import isclose
import pandas
import propka.lib
from propka.parameters import Parameters
from propka.molecular_container import MolecularContainer
from propka.input import read_parameter_file, read_molecule_file
from . import aa
from . import debump
from . import hydrogens
from . import forcefield
from . import protein as prot
from . import input_output as io
from .ligand.mol2 import Mol2Molecule
from . import input_output as io
from .config import VERSION, TITLE_FORMAT_STRING, CITATIONS, FORCE_FIELDS
from .config import REPAIR_LIMIT


_LOGGER = logging.getLogger("PDB2PQR%s" % VERSION)
_LOGGER.addFilter(io.DuplicateFilter())


# Round-off error when determining if charge is integral
CHARGE_ERROR = 1e-3


def build_parser():
    """Build an argument parser.

    Return:
        ArgumentParser() object
    """

    desc = TITLE_FORMAT_STRING.format(version=VERSION)
    pars = argparse.ArgumentParser(description=desc,
                                   formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    pars.add_argument("input_path",
                      help="Input PDB path or ID (to be retrieved from RCSB database")
    pars.add_argument("output_pqr", help="Output PQR path")
    pars.add_argument("--log-level", help="Logging level", default="INFO",
                      choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"])
    grp1 = pars.add_argument_group(title="Mandatory options",
                                   description="One of the following options must be used")
    grp1.add_argument("--ff", choices=[ff.upper() for ff in FORCE_FIELDS],
                      default="PARSE",
                      help="The forcefield to use.")
    grp1.add_argument("--userff",
                      help=("The user-created forcefield file to use. Requires "
                            "--usernames and overrides --ff"))
    grp1.add_argument("--clean", action='store_true', default=False,
                      help=("Do no optimization, atom addition, or parameter "
                            "assignment, just return the original PDB file in "
                            "aligned format. Overrides --ff and --userff"))
    grp2 = pars.add_argument_group(title="General options")
    grp2.add_argument('--nodebump', dest='debump', action='store_false',
                      default=True, help='Do not perform the debumping operation')
    grp2.add_argument('--noopt', dest='opt', action='store_false', default=True,
                      help='Do not perform hydrogen optimization')
    grp2.add_argument('--keep-chain', action='store_true', default=False,
                      help='Keep the chain ID in the output PQR file')
    grp2.add_argument('--assign-only', action='store_true', default=False,
                      help=("Only assign charges and radii - do not add atoms, "
                            "debump, or optimize."))
    grp2.add_argument('--ffout', choices=[ff.upper() for ff in FORCE_FIELDS],
                      help=('Instead of using the standard canonical naming '
                            'scheme for residue and atom names, use the names '
                            'from the given forcefield'))
    grp2.add_argument('--usernames',
                      help=('The user-created names file to use. Required if '
                            'using --userff'))
    grp2.add_argument('--apbs-input',
                      help=('Create a template APBS input file based on the '
                            'generated PQR file at the specified location.'))
    grp2.add_argument('--ligand',
                      help=('Calculate the parameters for the specified '
                            'MOL2-format ligand at the path specified by this '
                            'option.  PDB2PKA must be compiled.'))
    grp2.add_argument('--whitespace', action='store_true', default=False,
                      help=('Insert whitespaces between atom name and residue '
                            'name, between x and y, and between y and z.'))
    grp2.add_argument('--neutraln', action='store_true', default=False,
                      help=('Make the N-terminus of this protein neutral '
                            '(default is charged). Requires PARSE force field.'))
    grp2.add_argument('--neutralc', action='store_true', default=False,
                      help=('Make the C-terminus of this protein neutral '
                            '(default is charged). Requires PARSE force field.'))
    grp2.add_argument('--drop-water', action='store_true', default=False,
                      help='Drop waters before processing protein.')
    grp2.add_argument('--include-header', action='store_true', default=False,
                      help=('Include pdb header in pqr file. WARNING: The '
                            'resulting PQR file will not work with APBS versions '
                            'prior to 1.5'))
    grp3 = pars.add_argument_group(title="pKa options",
                                   description="Options for titration calculations")
    grp3.add_argument('--titration-state-method', dest="pka_method",
                      choices=('propka', 'pdb2pka'),
                      help=('Method used to calculate titration states. If a '
                            'titration state method is selected, titratable '
                            'residue charge states will be set by the pH value '
                            'supplied by --with_ph'))
    grp3.add_argument('--with-ph', dest='ph', type=float, action='store',
                      default=7.0,
                      help=('pH values to use when applying the results of the '
                            'selected pH calculation method.'))
    # TODO - need separate argparse groups for PDB2PKA and PROPKA
    # These exist but need real options
    grp4 = pars.add_argument_group(title="PDB2PKA method options")
    grp4.add_argument('--pdb2pka-out', default='pdb2pka_output',
                      help='Output directory for PDB2PKA results.')
    grp4.add_argument('--pdb2pka-resume', action="store_true", default=False,
                      help='Resume run from state saved in output directory.')
    grp4.add_argument('--pdie', default=8.0,
                      help='Protein dielectric constant.')
    grp4.add_argument('--sdie', default=80.0,
                      help='Solvent dielectric constant.')
    grp4.add_argument('--pairene', default=1.0,
                      help='Cutoff energy in kT for pairwise pKa interaction energies.')
    pars = propka.lib.build_parser(pars)
    return pars


def print_splash_screen(args):
    """Print argument overview and citation information.

    Args:
        args:  argparse namespace
    """
    _LOGGER.debug("Args:  %s", args)
    _LOGGER.info("%s", TITLE_FORMAT_STRING.format(version=VERSION))
    for citation in CITATIONS:
        _LOGGER.info(citation)


def check_files(args):
    """Check for other necessary files.

    Args:
        args:  argparse namespace
    Raises:
        FileNotFoundError:  necessary files not found
        RuntimeError:  input argument or file parsing problems
    """
    if args.usernames is not None:
        usernames = Path(args.usernames)
        if not usernames.is_file():
            error = "User-provided names file does not exist: %s" % usernames
            raise FileNotFoundError(error)

    if args.userff is not None:
        userff = Path(args.userff)
        if not userff.is_file():
            error = "User-provided forcefield file does not exist: %s" % userff
            raise FileNotFoundError(error)
        if args.usernames is None:
            raise RuntimeError('--usernames must be specified if using --userff')
    elif args.ff is not None:
        if io.test_dat_file(args.ff) == "":
            raise RuntimeError("Unable to load parameter file for forcefield %s" % args.ff)

    if args.ligand is not None:
        ligand = Path(args.ligand)
        if not ligand.is_file():
            error = "Unable to find ligand file: %s" % ligand
            raise FileNotFoundError(error)


def check_options(args):
    """Sanity check options.

    Args:
        args:  argparse namespace
    Raises:
        RuntimeError:  silly option combinations were encountered.
    """
    if (args.ph < 0) or (args.ph > 14):
        raise RuntimeError(("Specified pH (%s) is outside the range [1, 14] "
                            "of this program") % args.ph)

    if args.neutraln and (args.ff is None or args.ff.lower() != 'parse'):
        raise RuntimeError('--neutraln option only works with PARSE forcefield!')

    if args.neutralc and (args.ff is None or args.ff.lower() != 'parse'):
        raise RuntimeError('--neutralc option only works with PARSE forcefield!')


def print_pqr(args, pqr_lines, header_lines, missing_lines, is_cif):
    """Print output to specified file

    TODO - move this to another module (utilities)

    Args:
        args:  argparse namespace
        pqr_lines:  output lines (records)
        header_lines:  header lines
        missing_lines:  lines describing missing atoms (should go in header)
        is_cif:  flag indicating CIF-format
    """
    with open(args.output_pqr, "wt") as outfile:
        # Adding whitespaces if --whitespace is in the options
        if header_lines:
            _LOGGER.warning("Ignoring %d header lines in output.", len(header_lines))
        if missing_lines:
            _LOGGER.warning("Ignoring %d missing lines in output.", len(missing_lines))
        for line in pqr_lines:
            if args.whitespace:
                if line[0:4] == 'ATOM':
                    newline = line[0:6] + ' ' + line[6:16] + ' ' + \
                        line[16:38] + ' ' + line[38:46] + ' ' + line[46:]
                    outfile.write(newline)
                elif line[0:6] == 'HETATM':
                    newline = line[0:6] + ' ' + line[6:16] + ' ' + \
                        line[16:38] + ' ' + line[38:46] + ' ' + line[46:]
                    outfile.write(newline)
                elif line[0:3] == "TER" and is_cif:
                    pass
            else:
                if line[0:3] == "TER" and is_cif:
                    pass
                else:
                    outfile.write(line)
        if is_cif:
            outfile.write("#\n")


def transform_arguments(args):
    """Transform arguments with logic not provided by argparse.

    TODO - I wish this could be done with argparse.

    Args:
        args:  argparse namespace
    Returns:
        argparse namespace
    """
    if args.assign_only or args.clean:
        args.debump = False
        args.opt = False
    if args.userff is not None:
        args.userff = args.userff.lower()
    elif args.ff is not None:
        args.ff = args.ff.lower()
    if args.ffout is not None:
        args.ffout = args.ffout.lower()
    return args


def setup_molecule(pdblist, definition, ligand_path):
    """Set up the molecular system.

    Args:
        pdblist:  list of PDB records
        definition:  topology definition
        ligand_path:  path to ligand (may be None)
    Returns:
        protein:  protein object
        definition:  definition object (revised if ligand was parsed)
        ligand:  ligand object (may be None)
    """
    if ligand_path is not None:
        ligand = Mol2Molecule()
        with open(ligand_path, "rt", encoding="utf-8") as ligand_file:
            ligand.read(ligand_file)
    else:
        ligand = None
    protein = prot.Protein(pdblist, definition)
    _LOGGER.info("Created protein object with %d residues and %d atoms.",
                 len(protein.residues), len(protein.atoms))
    for residue in protein.residues:
        multoccupancy = False
        for atom in residue.atoms:
            if atom.alt_loc != "":
                multoccupancy = True
                txt = "Multiple occupancies found: %s in %s." % (atom.name, residue)
                _LOGGER.warning(txt)
        if multoccupancy:
            _LOGGER.warning(("Multiple occupancies found in %s. At least "
                             "one of the instances is being ignored."), residue)
    return protein, definition, ligand


def is_repairable(protein, has_ligand):
    """Determine if the protein can be (or needs to be) repaired.

    Args:
        protein:  protein object
        has_ligand:  does the system contain a ligand? (bool)
    Returns:
        Boolean
    Raises:
        ValueError if there are insufficient heavy atoms or a significant part of
        the protein is missing
    """
    num_heavy = protein.num_heavy
    num_missing = protein.num_missing_heavy
    if num_heavy == 0:
        if not has_ligand:
            raise ValueError(("No biomolecule heavy atoms found and no ligand "
                              "present.  Unable to proceed.  You may also see "
                              "this message if PDB2PQR does not have parameters "
                              "for any residue in your protein."))
        else:
            _LOGGER.warning(("No heavy atoms found but a ligand is present. "
                             "Proceeding with caution."))
            return False

    if num_missing == 0:
        _LOGGER.info("This biomolecule is clean.  No repair needed.")
        return False

    miss_frac = float(num_missing) / float(num_heavy)
    if miss_frac > REPAIR_LIMIT:
        error = "This PDB file is missing too many (%i out of " % num_missing
        error += "%i, %g) heavy atoms to accurately repair the file.  " % \
                    (num_heavy, miss_frac)
        error += "The current repair limit is set at %g. " % REPAIR_LIMIT
        error += "You may also see this message if PDB2PQR does not have "
        error += "parameters for enough residues in your protein."
        _LOGGER.error(error)
        return False
    return True


def drop_water(pdblist):
    """Drop waters from a list of PDB records.

    TODO - this module is already too long but this function fits better here.
    Other possible place would be utilities.

    Args:
        pdb_list:  list of PDB records as returned by io.get_molecule
    Returns:
        new list of PDB records with waters removed.
    """
    pdblist_new = []
    for record in pdblist:
        record_type = record.record_type()
        if record_type in ["HETATM", "ATOM", "SIGATM", "SEQADV"]:
            if record.res_name in aa.WAT.water_residue_names:
                continue
        pdblist_new.append(record)
    return pdblist_new


def run_propka(args, protein):
    """Run a PROPKA calculation.

    Args:
        args:  argparse namespace
        protein:  protein object
    Returns:
        1. DataFrame of assigned pKa values
        2. string with filename of PROPKA-created pKa file
    """
    # TODO - eliminate need to write temporary file
    lines = io.print_protein_atoms(
        atomlist=protein.atoms, chainflag=args.keep_chain,
        pdbfile=True)
    with NamedTemporaryFile(
            "wt", suffix=".pdb", delete=False) as pdb_file:
        for line in lines:
            pdb_file.write(line)
        pdb_path = pdb_file.name
    parameters = read_parameter_file(args.parameters, Parameters())
    molecule = MolecularContainer(parameters, args)
    molecule = read_molecule_file(pdb_path, molecule)
    molecule.calculate_pka()

    pka_filename = Path(pdb_path).stem + ".pka"
    molecule.write_pka(filename=pka_filename)

    conformation = molecule.conformations["AVR"]
    rows = []
    for group in conformation.groups:
        row_dict = OrderedDict()
        atom = group.atom
        row_dict["res_num"] = atom.res_num
        row_dict["res_name"] = atom.res_name
        row_dict["chain_id"] = atom.chain_id
        row_dict["group_label"] = group.label
        row_dict["group_type"] = getattr(group, "type", None)
        row_dict["pKa"] = group.pka_value
        row_dict["model_pKa"] = group.model_pka
        if group.coupled_titrating_group:
            row_dict["coupled_group"] = group.coupled_titrating_group.label
        else:
            row_dict["coupled_group"] = None
        rows.append(row_dict)
    df = pandas.DataFrame(rows)

    return df, pka_filename


def non_trivial(args, protein, ligand, definition, is_cif):
    """Perform a non-trivial PDB2PQR run.

    Args:
        args:  argparse namespace.
        protein:  Protein object.  This is not actually specific to proteins...
                  Nucleic acids are biomolecules, too!
        ligand:  Mol2Molecule object or None
        definition:  Definition object for topology.
        is_cif:  Boolean indicating whether file is CIF format.
    Returns:
        Dictionary with results.
        TODO - replace this with a more robust return option
    """
    _LOGGER.info("Loading forcefield.")
    forcefield_ = forcefield.Forcefield(args.ff, definition, args.userff,
                                        args.usernames)
    _LOGGER.info("Loading hydrogen topology definitions.")
    hydrogen_handler = hydrogens.create_handler()
    debumper = debump.Debump(protein)

    if args.assign_only:
        # TODO - I don't understand why HIS needs to be set to HIP for assign-only
        protein.set_hip()
    else:
        if is_repairable(protein, args.ligand is not None):
            _LOGGER.info("Attempting to repair %d missing atoms in biomolecule.",
                         protein.num_missing_heavy)
            protein.repair_heavy()

        _LOGGER.info("Updating disulfide bridges.")
        protein.update_ss_bridges()

        if args.debump:
            _LOGGER.info("Debumping biomolecule.")
            debumper.debump_protein()

        if args.pka_method == "propka":
            _LOGGER.info("Assigning titration states with PROPKA.")
            protein.remove_hydrogens()
            pka_df, pka_filename = run_propka(args, protein)

            protein.apply_pka_values(
                forcefield_.name, args.ph,
                dict(zip(pka_df.group_label, pka_df.pKa)))

        elif args.pka_method == "pdb2pka":
            _LOGGER.info("Assigning titration states with PDB2PKA.")
            raise NotImplementedError("PDB2PKA not implemented.")

        _LOGGER.info("Adding hydrogens to biomolecule.")
        protein.add_hydrogens()

        if args.debump:
            _LOGGER.info("Debumping biomolecule (again).")
            debumper.debump_protein()

        _LOGGER.info("Optimizing hydrogen bonds")
        hydrogen_routines = hydrogens.HydrogenRoutines(debumper, hydrogen_handler)
        if args.opt:
            hydrogen_routines.set_optimizeable_hydrogens()
            protein.hold_residues(None)
            hydrogen_routines.initialize_full_optimization()
            hydrogen_routines.optimize_hydrogens()
        else:
            hydrogen_routines.initialize_wat_optimization()
            hydrogen_routines.optimize_hydrogens()
        hydrogen_routines.cleanup()

    _LOGGER.info("Applying force field to biomolecule states.")
    protein.set_states()
    hitlist, misslist = protein.apply_force_field(forcefield_)

    missing_atoms = []
    lig_atoms = []

    if args.ligand is not None:
        _LOGGER.info("Processing ligand.")
        _LOGGER.warning("Using ZAP9 forcefield for ligand radii.")
        ligand.assign_parameters()
        for residue in protein.residues:
            tot_charge = 0
            for pdb_atom in residue.atoms:
                # Only check residues with HETATM
                if pdb_atom.type == "ATOM":
                    break
                try:
                    mol2_atom = ligand.atoms[pdb_atom.name]
                    pdb_atom.radius = mol2_atom.radius
                    pdb_atom.ffcharge = mol2_atom.charge
                    tot_charge += mol2_atom.charge
                    lig_atoms.append(pdb_atom)
                except KeyError:
                    err = (
                        "Can't find HETATM {r.name} {r.res_seq} {a.name} "
                        "in MOL2 file").format(r=residue, a=pdb_atom)
                    _LOGGER.warning(err)
                    missing_atoms.append(pdb_atom)

    matched_atoms = hitlist + lig_atoms

    for residue in protein.residues:
        if not isclose(
                residue.charge, int(residue.charge), abs_tol=CHARGE_ERROR):
            err = (
                "Residue {r.name} {r.res_seq} charge is "
                "non-integer: {r.charge}").format(r=residue)
            raise ValueError(err)

    if args.ffout is not None:
        _LOGGER.info("Applying custom naming scheme (%s).", args.ffout)
        if args.ffout != args.ff:
            name_scheme = forcefield.Forcefield(args.ffout, definition, None)
        else:
            name_scheme = forcefield_
        protein.apply_name_scheme(name_scheme)

    _LOGGER.info("Regenerating headers.")
    reslist, charge = protein.charge
    if is_cif:
        header = io.print_pqr_header_cif(
            missing_atoms, reslist, charge, args.ff, args.pka_method, args.ph,
            args.ffout, include_old_header=args.include_header)
    else:
        header = io.print_pqr_header(
            protein.pdblist, missing_atoms, reslist, charge, args.ff,
            args.pka_method, args.ph, args.ffout,
            include_old_header=args.include_header)

    _LOGGER.info("Regenerating PDB lines.")
    lines = io.print_protein_atoms(matched_atoms, args.keep_chain)

    return {"lines": lines, "header": header, "missed_residues": missing_atoms}


def main(args):
    """Main driver for running program from the command line.

    Validate inputs, launch PDB2PQR, handle output.

    Args:
        args:  argument namespace object (e.g., as returned by argparse).
    """
    logging.basicConfig(level=getattr(logging, args.log_level))
    _LOGGER.debug("Invoked with arguments: %s", args)
    print_splash_screen(args)

    _LOGGER.info("Checking and transforming input arguments.")
    args = transform_arguments(args)
    check_files(args)
    check_options(args)

    _LOGGER.info("Loading topology files.")
    definition = io.get_definitions()

    _LOGGER.info("Loading molecule: %s", args.input_path)
    pdblist, is_cif = io.get_molecule(args.input_path)

    if args.drop_water:
        _LOGGER.info("Dropping water from structure.")
        pdblist = drop_water(pdblist)
    _LOGGER.info("Setting up molecule.")
    protein, definition, ligand = setup_molecule(pdblist, definition, args.ligand)

    _LOGGER.info("Setting termini states for protein chains.")
    protein.set_termini(args.neutraln, args.neutralc)
    protein.update_bonds()

    if args.clean:
        _LOGGER.info("Arguments specified cleaning only; skipping remaining steps.")
        results = {"header": "", "missed_residues": None, "protein": protein,
                   "lines": io.print_protein_atoms(protein.atoms, args.keep_chain)}
    else:
        results = non_trivial(
            args=args, protein=protein, ligand=ligand, definition=definition,
            is_cif=is_cif)

    print_pqr(args=args, pqr_lines=results["lines"], header_lines=results["header"],
              missing_lines=results["missed_residues"], is_cif=is_cif)

    if args.apbs_input:
        raise NotImplementedError("Missing argument for APBS input file.")
        io.dump_apbs(args.output_pqr)
